"""
KALSHI BTC BOT — Directional BTC Up/Down Trading
Constantly trades on whether BTC is going up or down using
Kalshi's short-duration event contracts.
"""
import os
import sys
import asyncio
import json
import time
import base64
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.shared import (
    KALSHI_KEY_ID, KALSHI_PRIVATE_KEY,
    supabase, log_trade, update_trade, log_equity, get_bot_state, update_bot_state,
    update_pnl, send_alert, RiskManager, query_perplexity, AdaptiveParams,
)

BOT_ID = "kalshi_btc"
KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
TRADE_COOLDOWN_S = 300  # Don't trade more than once per 5 minutes
_last_trade_time = 0


# ─── KALSHI AUTH ──────────────────────────────────────────────────
def sign_kalshi_request(method: str, path: str, timestamp_ms: int) -> str:
    """Sign a Kalshi API request with RSA private key."""
    message = f"{timestamp_ms}{method}{path}"
    private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY.encode(), password=None
    )
    signature = private_key.sign(
        message.encode('utf-8'),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def kalshi_headers(method: str, path: str) -> dict:
    """Generate authenticated headers for Kalshi API."""
    ts = int(time.time() * 1000)
    # Kalshi requires the full path including /trade-api/v2 prefix in the signature
    full_path = f"/trade-api/v2{path}"
    sig = sign_kalshi_request(method, full_path, ts)
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
    }


# ─── KALSHI API CLIENT ────────────────────────────────────────────
class KalshiClient:
    def __init__(self):
        self.base = KALSHI_API_URL

    async def get_balance(self) -> float:
        path = "/portfolio/balance"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path), timeout=10)
            data = r.json()
            # Kalshi returns balance (cash) + portfolio_value (open positions) in cents
            cash = data.get("balance", 0)
            positions = data.get("portfolio_value", 0)
            return (cash + positions) / 100

    async def get_markets(self, series_ticker: str = None, status: str = "open") -> list:
        path = "/markets"
        params = {"status": status, "limit": 20}
        if series_ticker:
            params["series_ticker"] = series_ticker
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path),
                            params=params, timeout=10)
            return r.json().get("markets", [])

    async def get_market(self, ticker: str) -> dict:
        path = f"/markets/{ticker}"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path), timeout=10)
            return r.json().get("market", {})

    async def place_order(self, ticker: str, side: str, count: int, 
                          yes_price: int = None, no_price: int = None) -> dict:
        """Place an order. Side: 'yes' or 'no'. Price in cents (1-99)."""
        path = "/portfolio/orders"
        order = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "market",
        }
        if yes_price and side == "yes":
            order["type"] = "limit"
            order["yes_price"] = yes_price
        if no_price and side == "no":
            order["type"] = "limit"
            order["no_price"] = no_price

        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self.base}{path}", headers=kalshi_headers("POST", path),
                             json=order, timeout=10)
            return r.json()

    async def get_positions(self) -> list:
        path = "/portfolio/positions"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path), timeout=10)
            return r.json().get("market_positions", [])

    async def get_orders(self, status: str = "settled", limit: int = 50) -> list:
        path = "/portfolio/orders"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path),
                            params={"status": status, "limit": limit}, timeout=10)
            return r.json().get("orders", [])


# ─── BTC PRICE FEED ───────────────────────────────────────────────
async def get_btc_price() -> float:
    """Get current BTC price from Binance."""
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.binance.us/api/v3/ticker/price",
                        params={"symbol": "BTCUSDT"}, timeout=5)
        return float(r.json()["price"])


async def get_btc_momentum(lookback_minutes: int = 15) -> dict:
    """Get BTC price momentum over recent window."""
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.binance.us/api/v3/klines",
                        params={"symbol": "BTCUSDT", "interval": "1m", "limit": lookback_minutes},
                        timeout=5)
        klines = r.json()
        closes = [float(k[4]) for k in klines]
        if len(closes) < 2:
            return {"direction": "NEUTRAL", "change_pct": 0, "confidence": 0.5}

        change_pct = ((closes[-1] - closes[0]) / closes[0]) * 100
        # Simple momentum: strong moves = higher confidence
        abs_change = abs(change_pct)
        if abs_change > 0.5:
            confidence = min(0.95, 0.6 + abs_change * 0.2)
        elif abs_change > 0.2:
            confidence = 0.55 + abs_change * 0.15
        else:
            confidence = 0.50

        direction = "UP" if change_pct > 0.05 else "DOWN" if change_pct < -0.05 else "NEUTRAL"

        return {
            "direction": direction,
            "change_pct": round(change_pct, 4),
            "confidence": round(confidence, 3),
            "current_price": closes[-1],
            "start_price": closes[0],
        }


# ─── TRADE SETTLEMENT & LEARNING ─────────────────────────────────
async def settle_trades(client: KalshiClient, adaptive: AdaptiveParams):
    """Check Kalshi for settled orders, update DB, then retune params."""
    try:
        settled_orders = await client.get_orders(status="settled", limit=50)
        if not settled_orders:
            return

        # Load our FILLED trades that haven't been closed yet
        res = supabase.table("trades").select("*") \
            .eq("bot_id", BOT_ID).eq("status", "FILLED").execute()
        open_trades = res.data or []
        if not open_trades:
            return

        # Index open trades by order_id stored in metadata
        by_order_id = {}
        for t in open_trades:
            oid = (t.get("metadata") or {}).get("order_id")
            if oid:
                by_order_id[oid] = t

        for order in settled_orders:
            order_id = order.get("order_id") or order.get("id")
            trade = by_order_id.get(order_id)
            if not trade:
                continue

            # Kalshi payout is in cents
            payout_cents = order.get("payout", 0)
            qty = trade.get("quantity") or 1
            entry = trade.get("entry_price") or 0.50
            cost = qty * entry
            pnl = round((payout_cents / 100) - cost, 4)
            won = payout_cents > 0

            update_trade(
                trade["id"],
                status="WON" if won else "LOST",
                exit_price=round(payout_cents / 100 / qty, 4),
                pnl=pnl,
                closed_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  📊 Settled: {trade['ticker']} {'✅ WON' if won else '❌ LOST'} | P&L: ${pnl:+.4f}")

        # Retune params based on updated outcomes
        adaptive.tune()

    except Exception as e:
        print(f"  Settlement error: {e}")


# ─── TRADING LOGIC ────────────────────────────────────────────────
async def find_btc_contracts(client: KalshiClient) -> list:
    """Find active BTC up/down contracts on Kalshi."""
    try:
        # Try series-specific search first (KXBTCD = BTC daily contracts)
        for series in ["KXBTCD", "KXBTC", "BTCD"]:
            markets = await client.get_markets(series_ticker=series)
            if markets:
                print(f"  Found {len(markets)} contracts via series {series}")
                return markets

        # Fallback: scan all open markets and filter for BTC
        markets = await client.get_markets()
        print(f"  Scanning {len(markets)} open markets for BTC...")
        btc_markets = [
            m for m in markets
            if "btc" in m.get("ticker", "").lower()
            or "btc" in m.get("title", "").lower()
            or "bitcoin" in m.get("title", "").lower()
        ]
        print(f"  Found {len(btc_markets)} BTC contracts")
        return btc_markets
    except Exception as e:
        print(f"  Error fetching markets: {e}")
        return []


async def execute_trade(client: KalshiClient, risk: RiskManager,
                        momentum: dict, balance: float):
    """Execute a single BTC directional trade."""
    global _last_trade_time
    if momentum["direction"] == "NEUTRAL":
        print(f"  Momentum neutral ({momentum['change_pct']:+.4f}%) — skipping")
        return

    # Cooldown: don't spam orders
    elapsed = time.time() - _last_trade_time
    if elapsed < TRADE_COOLDOWN_S:
        print(f"  Cooldown active — {int(TRADE_COOLDOWN_S - elapsed)}s remaining")
        return

    # Find suitable contract
    contracts = await find_btc_contracts(client)
    if not contracts:
        print("  No BTC contracts available")
        return

    # Sort by expiry — nearest first (soonest to resolve = most price sensitivity)
    contracts.sort(key=lambda m: m.get("close_time") or m.get("expiration_time") or "")
    contract = contracts[0]
    ticker = contract.get("ticker", "")
    title = contract.get("title", "").lower()

    # Determine contract direction from title, then pick side accordingly
    contract_asks_up = any(w in title for w in ["above", "higher", "over", "exceed"])
    if momentum["direction"] == "UP":
        side = "yes" if contract_asks_up else "no"
    else:
        side = "no" if contract_asks_up else "yes"

    # Position size based on confidence — cap at 3 contracts max with $10 balance
    position_dollars = risk.calculate_position_size(
        balance,
        conviction=momentum["confidence"],
        win_rate=0.58,
        avg_win=0.80,
        avg_loss=1.0,
    )
    contracts_count = max(1, min(3, int(position_dollars)))

    try:
        order = await client.place_order(
            ticker=ticker,
            side=side,
            count=contracts_count,
        )

        print(f"  [ORDER RESPONSE] {order}")
        # Check if order actually succeeded
        if order.get("error") or not order.get("order"):
            print(f"  ❌ Order rejected: {order}")
            return

        order_status = order.get("order", {}).get("status", "unknown")
        order_id = order.get("order", {}).get("order_id") or order.get("order", {}).get("id")
        filled = order_status in ("filled", "resting", "pending")
        _last_trade_time = time.time()

        log_trade(
            bot_id=BOT_ID,
            ticker=ticker,
            side=side.upper(),
            quantity=contracts_count,
            entry_price=0.50,
            strategy="BTC Directional",
            status="FILLED" if filled else order_status.upper(),
            order_type="MARKET",
            sentiment_score=momentum["confidence"],
            reasoning=f"BTC {momentum['direction']} momentum: {momentum['change_pct']:+.4f}% over 15m, confidence {momentum['confidence']:.2f}",
            metadata={**momentum, "order_id": order_id},
        )

        print(f"  ✅ {side.upper()} {contracts_count}x {ticker} "
              f"(BTC {momentum['direction']} {momentum['change_pct']:+.4f}%, "
              f"conf: {momentum['confidence']:.2f}) — order status: {order_status}")

    except Exception as e:
        print(f"  Order error: {e}")


# ─── MAIN LOOP ────────────────────────────────────────────────────
async def main():
    print(f"🚀 Kalshi BTC Directional Bot starting...")

    client = KalshiClient()
    risk = RiskManager(BOT_ID, daily_limit_pct=-20.0, weekly_limit_pct=-30.0, monthly_kill_pct=-40.0)
    adaptive = AdaptiveParams(BOT_ID)
    params = adaptive.load()
    print(f"   Loaded params: {params}")

    # Verify connection and write initial balance to dashboard
    try:
        balance = await client.get_balance()
        print(f"   Balance: ${balance:,.2f}")
        update_pnl(BOT_ID, balance)
    except Exception as e:
        import traceback
        print(f"   Auth error: {e}")
        print(f"   Traceback: {traceback.format_exc()}")
        balance = 0

    # Ensure bot is active on startup
    state = get_bot_state(BOT_ID)
    if not state.get("active"):
        print(f"   Bot was paused — resuming automatically on startup")
        update_bot_state(BOT_ID, active=True, mode="AGGRESSIVE")

    while True:
        state = get_bot_state(BOT_ID)
        if not state.get("active"):
            print(f"[{BOT_ID}] Paused. Sleeping 60s...")
            await asyncio.sleep(60)
            continue

        update_bot_state(BOT_ID, last_heartbeat=datetime.now(timezone.utc).isoformat())

        # Risk check
        risk_check = risk.check_limits()
        if risk_check["action"] != "OK":
            print(f"[{BOT_ID}] Risk: {risk_check['reason']}")
            await asyncio.sleep(300)
            continue

        try:
            # Get current balance and update P&L metrics
            balance = await client.get_balance()
            update_pnl(BOT_ID, balance)

            # Check for settled trades and retune params
            await settle_trades(client, adaptive)
            params = adaptive.load()

            # Get BTC momentum using learned lookback
            momentum = await get_btc_momentum(lookback_minutes=params["lookback_minutes"])
            btc_price = momentum.get("current_price", 0)
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"BTC: ${btc_price:,.2f} | {momentum['direction']} "
                  f"({momentum['change_pct']:+.4f}%) | Conf: {momentum['confidence']:.2f} "
                  f"| Threshold: {params['confidence_threshold']:.2f}")

            # Use learned confidence threshold
            if momentum["confidence"] >= params["confidence_threshold"] and momentum["direction"] != "NEUTRAL":
                await execute_trade(client, risk, momentum, balance)
            else:
                print(f"  Below confidence threshold — waiting")

        except Exception as e:
            print(f"  Error: {e}")

        # Check every 5 minutes
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(main())
