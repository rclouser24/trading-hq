"""
KALSHI ARB BOT — Latency Arbitrage on Kalshi Event Contracts
Monitors Binance CEX price feeds and exploits the lag between
CEX price moves and Kalshi order book repricing.
Based on the 0x8dxd strategy adapted for Kalshi's CFTC-regulated exchange.
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
    supabase, log_trade, update_trade, log_arb_metric, get_bot_state, update_bot_state,
    update_pnl, send_alert, RiskManager, AdaptiveParams,
)

BOT_ID = "kalshi_arb"
KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Arb parameters
EDGE_THRESHOLD_PCT = 3.0       # Minimum edge to trigger a trade
MAX_POSITION_PCT = 0.08        # 8% of portfolio per trade
KELLY_FRACTION = 0.25          # Quarter-Kelly for safety
MIN_LIQUIDITY = 50000          # Minimum contract liquidity


# ─── KALSHI AUTH (same as btc bot) ────────────────────────────────
def sign_kalshi_request(method: str, path: str, timestamp_ms: int) -> str:
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


# ─── KALSHI CLIENT ────────────────────────────────────────────────
class KalshiArbClient:
    def __init__(self):
        self.base = KALSHI_API_URL

    async def get_balance(self) -> float:
        path = "/portfolio/balance"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path), timeout=5)
            data = r.json()
            # Kalshi returns balance (cash) + portfolio_value (open positions) in cents
            cash = data.get("balance", 0)
            positions = data.get("portfolio_value", 0)
            return (cash + positions) / 100

    async def get_orderbook(self, ticker: str) -> dict:
        path = f"/markets/{ticker}/orderbook"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path), timeout=5)
            return r.json().get("orderbook", {})

    async def get_btc_markets(self) -> list:
        # Try known BTC series tickers first
        for series in ["KXBTCD", "KXBTC", "BTCD"]:
            path = "/markets"
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path),
                                params={"status": "open", "series_ticker": series, "limit": 50}, timeout=10)
                markets = r.json().get("markets", [])
                if markets:
                    return markets

        # Fallback: scan all and filter
        path = "/markets"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path),
                            params={"status": "open", "limit": 100}, timeout=10)
            markets = r.json().get("markets", [])
            return [m for m in markets if
                    "btc" in m.get("ticker", "").lower()
                    or "btc" in m.get("title", "").lower()
                    or "bitcoin" in m.get("title", "").lower()]

    async def get_orders(self, status: str = "settled", limit: int = 50) -> list:
        path = "/portfolio/orders"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path),
                            params={"status": status, "limit": limit}, timeout=10)
            return r.json().get("orders", [])

    async def place_order(self, ticker: str, side: str, count: int) -> dict:
        path = "/portfolio/orders"
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self.base}{path}", headers=kalshi_headers("POST", path),
                             json={
                                 "ticker": ticker,
                                 "action": "buy",
                                 "side": side,
                                 "count": count,
                                 "type": "market",
                             }, timeout=5)
            return r.json()


# ─── BINANCE PRICE FEED ──────────────────────────────────────────
class BinanceFeed:
    """High-frequency BTC price monitoring from Binance."""

    def __init__(self):
        self.last_price = 0
        self.last_update = 0
        self.price_history = []  # (timestamp, price) tuples

    async def get_price(self) -> float:
        """Get current BTC/USDT price from Binance REST API."""
        async with httpx.AsyncClient() as c:
            r = await c.get("https://api.binance.us/api/v3/ticker/price",
                            params={"symbol": "BTCUSDT"}, timeout=3)
            price = float(r.json()["price"])
            now = time.time()
            self.last_price = price
            self.last_update = now
            self.price_history.append((now, price))
            # Keep last 5 minutes of history
            cutoff = now - 300
            self.price_history = [(t, p) for t, p in self.price_history if t > cutoff]
            return price

    def get_recent_move(self, seconds: int = 30) -> dict:
        """Calculate price move over recent window."""
        if len(self.price_history) < 2:
            return {"direction": "NEUTRAL", "change_pct": 0, "magnitude": 0}

        cutoff = time.time() - seconds
        recent = [(t, p) for t, p in self.price_history if t > cutoff]
        if len(recent) < 2:
            return {"direction": "NEUTRAL", "change_pct": 0, "magnitude": 0}

        start_price = recent[0][1]
        end_price = recent[-1][1]
        change_pct = ((end_price - start_price) / start_price) * 100

        return {
            "direction": "UP" if change_pct > 0.02 else "DOWN" if change_pct < -0.02 else "NEUTRAL",
            "change_pct": round(change_pct, 4),
            "magnitude": abs(change_pct),
            "start": start_price,
            "end": end_price,
        }


# ─── ARB DETECTION ENGINE ────────────────────────────────────────
class ArbDetector:
    """Detects arbitrage opportunities between Binance price and Kalshi contracts."""

    def __init__(self, edge_threshold: float = EDGE_THRESHOLD_PCT):
        self.edge_threshold = edge_threshold
        self.last_trade_time = 0
        self.min_trade_interval = 10  # seconds between trades

    def calculate_edge(self, binance_move: dict, kalshi_implied_prob: float,
                       contract_direction: str) -> dict:
        """
        Calculate the edge between what Binance price implies and
        what Kalshi's contract is currently priced at.

        binance_move: recent price movement from Binance
        kalshi_implied_prob: current YES price on Kalshi (0-1)
        contract_direction: 'up' or 'down' — what the contract asks
        """
        if binance_move["direction"] == "NEUTRAL":
            return {"edge": 0, "side": None, "tradeable": False}

        # Estimate true probability based on Binance momentum
        # Strong moves (>0.3%) in the contract direction → high probability
        magnitude = binance_move["magnitude"]
        if magnitude > 0.5:
            true_prob = 0.85
        elif magnitude > 0.3:
            true_prob = 0.75
        elif magnitude > 0.15:
            true_prob = 0.65
        else:
            true_prob = 0.55

        # Determine if we should buy YES or NO
        binance_says_up = binance_move["direction"] == "UP"
        contract_asks_up = contract_direction == "up"

        if binance_says_up == contract_asks_up:
            # Binance agrees with contract direction → buy YES
            # Edge = how much we think YES is underpriced
            edge = (true_prob - kalshi_implied_prob) * 100
            side = "yes"
        else:
            # Binance disagrees with contract direction → buy NO
            # NO implied prob = 1 - yes_ask_price; edge = how much NO is underpriced
            no_implied_prob = 1 - kalshi_implied_prob
            true_no_prob = 1 - true_prob
            edge = (true_no_prob - no_implied_prob) * 100
            side = "no"

        tradeable = (edge >= self.edge_threshold and
                     time.time() - self.last_trade_time >= self.min_trade_interval)

        return {
            "edge": round(edge, 2),
            "side": side,
            "tradeable": tradeable,
            "true_prob": true_prob,
            "kalshi_prob": kalshi_implied_prob,
            "binance_direction": binance_move["direction"],
        }


# ─── TRADE SETTLEMENT ────────────────────────────────────────────
async def settle_trades(client: KalshiArbClient, adaptive: AdaptiveParams):
    """Check Kalshi for settled orders, update DB trade records, retune params."""
    try:
        settled_orders = await client.get_orders(status="settled", limit=50)
        if not settled_orders:
            return

        res = supabase.table("trades").select("*") \
            .eq("bot_id", BOT_ID).eq("status", "FILLED").execute()
        open_trades = res.data or []
        if not open_trades:
            return

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

            payout_cents = order.get("payout", 0)
            qty = trade.get("quantity") or 1
            entry = trade.get("entry_price") or 0.50
            pnl = round((payout_cents / 100) - (qty * entry), 4)
            won = payout_cents > 0

            update_trade(
                trade["id"],
                status="WON" if won else "LOST",
                exit_price=round(payout_cents / 100 / qty, 4),
                pnl=pnl,
                closed_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  📊 Settled: {trade['ticker']} {'✅ WON' if won else '❌ LOST'} | P&L: ${pnl:+.4f}")

        adaptive.tune()

    except Exception as e:
        print(f"  Settlement error: {e}")


# ─── MAIN ARB LOOP ───────────────────────────────────────────────
async def main():
    print(f"🚀 Kalshi Latency Arb Bot starting...")
    print(f"   Edge threshold: {EDGE_THRESHOLD_PCT}%")
    print(f"   Max position: {MAX_POSITION_PCT*100}%")
    print(f"   Kelly fraction: {KELLY_FRACTION}x")

    client = KalshiArbClient()
    feed = BinanceFeed()
    adaptive = AdaptiveParams(BOT_ID)
    params = adaptive.load()
    detector = ArbDetector(edge_threshold=params["edge_threshold"])
    risk = RiskManager(BOT_ID, daily_limit_pct=-20.0, weekly_limit_pct=-30.0, monthly_kill_pct=-40.0)
    print(f"   Loaded params: {params}")

    # Verify connection and write initial balance to dashboard
    try:
        balance = await client.get_balance()
        print(f"   Kalshi balance: ${balance:,.2f}")
        update_pnl(BOT_ID, balance)
    except Exception as e:
        print(f"   Kalshi auth error: {e}")
        balance = 0

    # Ensure bot is active on startup
    state = get_bot_state(BOT_ID)
    if not state.get("active"):
        print(f"   Bot was paused — resuming automatically on startup")
        update_bot_state(BOT_ID, active=True, mode="AGGRESSIVE")

    trade_count = 0
    win_count = 0

    while True:
        state = get_bot_state(BOT_ID)
        if not state.get("active"):
            print(f"[{BOT_ID}] Paused.")
            await asyncio.sleep(30)
            continue

        update_bot_state(BOT_ID, last_heartbeat=datetime.now(timezone.utc).isoformat())

        # Risk check
        risk_check = risk.check_limits()
        if risk_check["action"] != "OK":
            await asyncio.sleep(60)
            continue

        try:
            # Get current balance and update P&L metrics
            balance = await client.get_balance()
            update_pnl(BOT_ID, balance)

            # Check settled trades and retune — update detector threshold if changed
            await settle_trades(client, adaptive)
            new_params = adaptive.load()
            if new_params["edge_threshold"] != detector.edge_threshold:
                print(f"  🧠 Updating edge threshold: {detector.edge_threshold}→{new_params['edge_threshold']}%")
                detector.edge_threshold = new_params["edge_threshold"]

            # 1. Get current Binance price
            t_start = time.time()
            btc_price = await feed.get_price()
            binance_latency = (time.time() - t_start) * 1000

            # 2. Get recent price movement
            move = feed.get_recent_move(seconds=30)

            # 3. Find active BTC contracts on Kalshi
            contracts = await client.get_btc_markets()

            for contract in contracts[:5]:  # Check top 5 contracts
                ticker = contract.get("ticker", "")
                title = contract.get("title", "")

                # Get Kalshi implied probability from orderbook best ask (what we'd pay)
                try:
                    orderbook = await client.get_orderbook(ticker)
                    # yes_ask is what we pay to buy YES; yes_bid is what we receive selling YES
                    asks = orderbook.get("yes", [])
                    yes_ask_cents = asks[0][0] if asks else contract.get("yes_ask", contract.get("yes_bid", 50))
                    yes_price = yes_ask_cents / 100  # Convert cents to 0-1
                    if yes_price <= 0 or yes_price >= 1:
                        continue  # invalid price, skip
                except Exception:
                    continue

                # Determine contract direction from title
                is_up_contract = any(w in title.lower() for w in ["above", "higher", "up", "over"])
                contract_dir = "up" if is_up_contract else "down"

                # 4. Calculate edge
                arb = detector.calculate_edge(move, yes_price, contract_dir)

                # 5. Log metrics
                log_arb_metric(
                    lag_s=round(binance_latency / 1000, 3),
                    edge_pct=arb["edge"],
                    btc_binance=btc_price,
                    btc_kalshi_implied=yes_price * btc_price,
                    spread=arb["edge"],
                    action="TRADED" if arb["tradeable"] else "SKIPPED",
                )

                if not arb["tradeable"]:
                    continue

                # 6. Size the position
                balance = await client.get_balance()
                position_dollars = balance * MAX_POSITION_PCT * KELLY_FRACTION
                contracts_count = max(1, int(position_dollars))

                # 7. Execute
                t_exec_start = time.time()
                order = await client.place_order(ticker, arb["side"], contracts_count)
                exec_latency = int((time.time() - t_exec_start) * 1000)

                detector.last_trade_time = time.time()
                trade_count += 1

                print(f"  [ORDER RESPONSE] {order}")
                if order.get("error") or not order.get("order"):
                    print(f"  ❌ Order rejected: {order}")
                    break
                order_status = order.get("order", {}).get("status", "unknown")
                order_id = order.get("order", {}).get("order_id") or order.get("order", {}).get("id")

                log_trade(
                    bot_id=BOT_ID,
                    ticker=ticker,
                    side=arb["side"].upper(),
                    quantity=contracts_count,
                    entry_price=yes_price,
                    strategy="Latency Arb",
                    status="FILLED" if order_status in ("filled", "resting", "pending") else order_status.upper(),
                    order_type="MARKET",
                    edge_pct=arb["edge"],
                    latency_ms=exec_latency,
                    reasoning=f"Edge {arb['edge']:.1f}% | BTC {move['direction']} {move['change_pct']:+.4f}% | "
                              f"Kalshi implied {yes_price:.2f} vs true {arb['true_prob']:.2f}",
                    metadata={"arb": arb, "move": move, "binance_latency_ms": binance_latency, "order_id": order_id},
                )

                print(f"  ⚡ ARB TRADE #{trade_count}: {arb['side'].upper()} {contracts_count}x {ticker} "
                      f"| Edge: {arb['edge']:.1f}% | Exec: {exec_latency}ms "
                      f"| BTC {move['direction']} {move['change_pct']:+.4f}%")

                send_alert(BOT_ID, "ARB_TRADE",
                           f"⚡ {arb['side'].upper()} {contracts_count}x {ticker}\n"
                           f"Edge: {arb['edge']:.1f}% | Latency: {exec_latency}ms\n"
                           f"BTC: ${btc_price:,.0f} ({move['direction']} {move['change_pct']:+.4f}%)")

                break  # One trade per cycle

            if not contracts:
                print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"BTC: ${btc_price:,.2f} | {move['direction']} {move['change_pct']:+.4f}% | No contracts")

        except Exception as e:
            print(f"  Error: {e}")

        # High frequency: check every 5 seconds
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
