"""
KALSHI BTC BOT — Directional BTC Up/Down Trading
Strategies adopted from Polymarket research:
  1. Mid-window exit: sell at profit target or stop loss instead of holding to expiry
  2. Binance WebSocket: live price feed with zero polling lag
  3. Order book imbalance: YES/NO volume bias as entry confirmation
  4. Time-of-day filter: only trade during high-volatility windows
  5. 5-minute markets: target shortest-duration contracts for maximum signal relevance
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
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.shared import (
    KALSHI_KEY_ID, KALSHI_PRIVATE_KEY,
    supabase, log_trade, update_trade, log_equity, get_bot_state, update_bot_state,
    update_pnl, send_alert, RiskManager, query_perplexity, AdaptiveParams,
)

BOT_ID = "kalshi_btc"
KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
TRADE_COOLDOWN_S = 300      # Minimum seconds between new entries
PROFIT_TARGET   = 0.35      # Exit if contract price moves +35% in our favor
STOP_LOSS       = 0.30      # Exit if contract price moves -30% against us
MIN_CHANGE_PCT  = 0.20      # BTC must have moved at least 0.20% to consider entry
MAX_DAILY_TRADES = 10       # Hard cap — protect $10 balance from over-trading
_last_trade_time = 0
_daily_trade_count = 0
_daily_trade_date = ""

# High-volatility UTC hour windows — only trade during these periods
# (Asian open, London open, US open, US close)
HIGH_ACTIVITY_WINDOWS_UTC = [(0, 2), (8, 10), (13, 16), (20, 22)]


# ─── TIME-OF-DAY FILTER ───────────────────────────────────────────
def is_high_activity_hour() -> bool:
    hour = datetime.now(timezone.utc).hour
    return any(start <= hour < end for start, end in HIGH_ACTIVITY_WINDOWS_UTC)


# ─── KALSHI AUTH ──────────────────────────────────────────────────
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
            cash = data.get("balance", 0)
            positions = data.get("portfolio_value", 0)
            return (cash + positions) / 100

    async def get_markets(self, series_ticker: str = None, status: str = "open") -> list:
        path = "/markets"
        params = {"status": status, "limit": 50}
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

    async def get_orderbook(self, ticker: str) -> dict:
        path = f"/markets/{ticker}/orderbook"
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}{path}", headers=kalshi_headers("GET", path), timeout=5)
            return r.json().get("orderbook", {})

    async def place_order(self, ticker: str, action: str, side: str, count: int,
                          yes_price: int = None, no_price: int = None) -> dict:
        """Place a buy or sell order. action: 'buy' or 'sell'. Price in cents (1-99)."""
        path = "/portfolio/orders"
        order = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": "market",
        }
        if yes_price is not None and side == "yes":
            order["type"] = "limit"
            order["yes_price"] = yes_price
        if no_price is not None and side == "no":
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


# ─── BINANCE LIVE WEBSOCKET FEED ──────────────────────────────────
class BinanceLiveFeed:
    """Maintains a live BTC/USDT price history via Binance WebSocket.
    Runs as a persistent background task — no REST polling lag."""

    def __init__(self):
        self.price = 0.0
        self.price_history: list = []  # (timestamp, price) tuples, 30-min window
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        uri = "wss://stream.binance.us:9443/ws/btcusdt@aggTrade"
        while True:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    print("  📡 Binance WebSocket connected")
                    async for raw in ws:
                        data = json.loads(raw)
                        price = float(data.get("p", 0))
                        if price > 0:
                            now = time.time()
                            self.price = price
                            self.price_history.append((now, price))
                            # Keep last 30 minutes
                            cutoff = now - 1800
                            self.price_history = [(t, p) for t, p in self.price_history if t > cutoff]
            except Exception as e:
                print(f"  Binance WS disconnected: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    def get_momentum(self, lookback_seconds: int = 900) -> dict:
        """Compute momentum over lookback_seconds using live price history."""
        if len(self.price_history) < 2 or self.price == 0:
            return {"direction": "NEUTRAL", "change_pct": 0, "confidence": 0.5,
                    "current_price": self.price}

        cutoff = time.time() - lookback_seconds
        recent = [(t, p) for t, p in self.price_history if t > cutoff]
        if len(recent) < 2:
            recent = self.price_history[-2:]

        start_price = recent[0][1]
        end_price = recent[-1][1]
        change_pct = ((end_price - start_price) / start_price) * 100
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
            "current_price": end_price,
            "start_price": start_price,
        }


# ─── ORDER BOOK IMBALANCE SIGNAL ─────────────────────────────────
async def get_orderbook_signal(client: KalshiClient, ticker: str) -> dict:
    """Compute YES/NO volume imbalance from the order book.
    Kalshi orderbook format:
      {"yes": [[price_cents, size], ...], "no": [[price_cents, size], ...]}
    YES levels = bids to buy YES (bullish sentiment)
    NO levels  = bids to buy NO  (bearish sentiment)
    Imbalance > 0.6 means strong bullish pressure."""
    try:
        ob = await client.get_orderbook(ticker)
        print(f"  [OB RAW] keys={list(ob.keys())} yes_count={len(ob.get('yes',[]))} no_count={len(ob.get('no',[]))}")

        yes_levels = ob.get("yes", [])
        no_levels  = ob.get("no", [])

        # Kalshi levels: [[price_cents, size], ...]
        # YES levels sorted highest bid first; NO levels sorted highest bid first
        yes_vol = sum(int(lvl[1]) for lvl in yes_levels[:10] if len(lvl) >= 2)
        no_vol  = sum(int(lvl[1]) for lvl in no_levels[:10]  if len(lvl) >= 2)
        total = yes_vol + no_vol

        if total == 0:
            # Fallback: use market-level yes_bid/no_bid from contract data
            return {"imbalance": 0.5, "bias": "NEUTRAL", "yes_vol": 0, "no_vol": 0,
                    "yes_ask_cents": 50, "no_ask_cents": 50}

        imbalance = yes_vol / total
        bias = "BULLISH" if imbalance > 0.60 else "BEARISH" if imbalance < 0.40 else "NEUTRAL"

        # Best ask = lowest price level on the opposite side (what we'd pay to buy)
        # YES ask = lowest NO bid price mirrored (complementary), or just use yes_levels[-1]
        yes_ask_cents = yes_levels[-1][0] if yes_levels else 50   # worst (highest) yes bid ≈ ask
        no_ask_cents  = no_levels[-1][0]  if no_levels  else 50

        return {
            "imbalance": round(imbalance, 3),
            "bias": bias,
            "yes_vol": yes_vol,
            "no_vol": no_vol,
            "yes_ask_cents": yes_ask_cents,
            "no_ask_cents": no_ask_cents,
        }
    except Exception as e:
        print(f"  Orderbook signal error: {e}")
        return {"imbalance": 0.5, "bias": "NEUTRAL", "yes_vol": 0, "no_vol": 0,
                "yes_ask_cents": 50, "no_ask_cents": 50}


# ─── 5-MINUTE CONTRACT FINDER ────────────────────────────────────
async def find_5min_btc_contracts(client: KalshiClient) -> list:
    """Find the shortest-duration BTC contracts available.
    Tries 5-minute series first, then falls back to contracts
    expiring within 6 minutes, then all BTC contracts."""
    try:
        # Try known 5-minute series tickers
        for series in ["KXBTC5M", "KXBTC-5M", "KXBTCM5", "BTCM5", "KXBTCD", "KXBTC", "BTCD"]:
            markets = await client.get_markets(series_ticker=series)
            if markets:
                # Filter for contracts expiring soon if this is a 5m series
                if "5M" in series or "5m" in series or "M5" in series:
                    print(f"  Found {len(markets)} 5-min contracts via {series}")
                    return sorted(markets, key=lambda m: m.get("close_time") or "")
                else:
                    # For other series, prefer contracts expiring within 10 minutes
                    now = datetime.now(timezone.utc)
                    short = []
                    for m in markets:
                        close = m.get("close_time") or m.get("expiration_time") or ""
                        if close:
                            try:
                                expiry = datetime.fromisoformat(close.replace("Z", "+00:00"))
                                mins_left = (expiry - now).total_seconds() / 60
                                if 0.5 < mins_left <= 10:
                                    short.append(m)
                            except Exception:
                                pass
                    if short:
                        short.sort(key=lambda m: m.get("close_time") or "")
                        print(f"  Found {len(short)} short-duration contracts via {series}")
                        return short
                    # No short-expiry found — return all sorted
                    markets.sort(key=lambda m: m.get("close_time") or "")
                    print(f"  Found {len(markets)} contracts via {series}")
                    return markets

        # Final fallback: scan all open markets
        markets = await client.get_markets()
        btc = [m for m in markets if
               "btc" in m.get("ticker", "").lower()
               or "btc" in m.get("title", "").lower()
               or "bitcoin" in m.get("title", "").lower()]
        btc.sort(key=lambda m: m.get("close_time") or "")
        print(f"  Found {len(btc)} BTC contracts via full scan")
        return btc
    except Exception as e:
        print(f"  Contract search error: {e}")
        return []


# ─── MID-WINDOW POSITION MONITOR ─────────────────────────────────
async def monitor_and_exit(client: KalshiClient, ticker: str, side: str,
                            qty: int, entry_price_cents: int, trade_db_id: int):
    """Background task: watch an open position and exit early if profit/stop is hit.
    Runs every 15 seconds for up to 280 seconds (just before 5-min expiry)."""
    check_interval = 15
    max_runtime = 280
    start = time.time()

    print(f"  👁 Monitoring {ticker} {side.upper()} x{qty} @ {entry_price_cents}¢")

    while time.time() - start < max_runtime:
        await asyncio.sleep(check_interval)
        try:
            market = await client.get_market(ticker)
            if not market:
                continue

            # Current mid-price for our side
            if side == "yes":
                current_cents = market.get("yes_bid", entry_price_cents)
            else:
                current_cents = market.get("no_bid", entry_price_cents)

            if current_cents <= 0:
                continue

            change = (current_cents - entry_price_cents) / max(entry_price_cents, 1)
            print(f"  👁 {ticker}: entry={entry_price_cents}¢ current={current_cents}¢ "
                  f"change={change:+.1%} (target={PROFIT_TARGET:.0%} stop=-{STOP_LOSS:.0%})")

            if change >= PROFIT_TARGET:
                print(f"  💰 PROFIT TARGET HIT — exiting {ticker}")
                await _sell_position(client, ticker, side, qty,
                                     current_cents, entry_price_cents, trade_db_id, "PROFIT_EXIT")
                return

            if change <= -STOP_LOSS:
                print(f"  🛑 STOP LOSS HIT — exiting {ticker}")
                await _sell_position(client, ticker, side, qty,
                                     current_cents, entry_price_cents, trade_db_id, "STOP_EXIT")
                return

        except Exception as e:
            print(f"  Monitor error: {e}")

    print(f"  ⏱ {ticker} held to expiry — settlement will handle P&L")


async def _sell_position(client: KalshiClient, ticker: str, side: str, qty: int,
                          current_cents: int, entry_cents: int, trade_db_id: int, reason: str):
    """Place a limit sell order slightly below the current bid to ensure fill."""
    try:
        sell_price = max(1, current_cents - 2)
        order = await client.place_order(
            ticker=ticker, action="sell", side=side, count=qty,
            yes_price=sell_price if side == "yes" else None,
            no_price=sell_price if side == "no" else None,
        )
        print(f"  [SELL RESPONSE] {order}")

        exit_price = current_cents / 100
        pnl = round((exit_price * qty) - (entry_cents / 100 * qty), 4)

        update_trade(
            trade_db_id,
            status=reason,
            exit_price=exit_price,
            pnl=pnl,
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        print(f"  📤 Sold {qty}x {ticker} {side.upper()} @ {current_cents}¢ | P&L: ${pnl:+.4f}")
    except Exception as e:
        print(f"  Sell error: {e}")


# ─── TRADE SETTLEMENT & LEARNING ─────────────────────────────────
async def settle_trades(client: KalshiClient, adaptive: AdaptiveParams):
    """Match Kalshi settled orders to DB records, mark WON/LOST, retune params."""
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

        # Debug: print first settled order keys once to verify field names
        if settled_orders:
            sample = settled_orders[0]
            print(f"  [SETTLE DEBUG] keys={list(sample.keys())} order_id={sample.get('order_id')} id={sample.get('id')}")

        for order in settled_orders:
            order_id = (order.get("order_id") or order.get("id")
                        or order.get("client_order_id"))
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


# ─── TRADING LOGIC ────────────────────────────────────────────────
async def execute_trade(client: KalshiClient, risk: RiskManager,
                         momentum: dict, ob_signal: dict, balance: float):
    """Evaluate all signals and execute a trade with mid-window monitor."""
    global _last_trade_time, _daily_trade_count, _daily_trade_date

    # 0. Daily trade cap
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_trade_date != today:
        _daily_trade_count = 0
        _daily_trade_date = today
        print(f"  📅 New day — trade count reset")
    if _daily_trade_count >= MAX_DAILY_TRADES:
        print(f"  ⚠️ Daily cap reached ({_daily_trade_count}/{MAX_DAILY_TRADES}) — resuming tomorrow")
        return

    # 1. Time-of-day filter
    if not is_high_activity_hour():
        hour = datetime.now(timezone.utc).hour
        print(f"  ⏰ Outside high-activity window (UTC hour {hour}) — waiting")
        return

    # 2. Cooldown
    elapsed = time.time() - _last_trade_time
    if elapsed < TRADE_COOLDOWN_S:
        print(f"  Cooldown active — {int(TRADE_COOLDOWN_S - elapsed)}s remaining")
        return

    # 3. Find 5-minute contract
    contracts = await find_5min_btc_contracts(client)
    if not contracts:
        print("  No BTC contracts available")
        return

    contract = contracts[0]
    ticker = contract.get("ticker", "")
    title = contract.get("title", "").lower()

    # 4. Determine side from contract title + momentum direction
    contract_asks_up = any(w in title for w in ["above", "higher", "over", "exceed"])
    if momentum["direction"] == "UP":
        side = "yes" if contract_asks_up else "no"
    else:
        side = "no" if contract_asks_up else "yes"

    # 5. Order book confirmation: bias must agree or be neutral
    ob_bias = ob_signal.get("bias", "NEUTRAL")
    momentum_dir = momentum["direction"]
    if ob_bias == "BULLISH" and momentum_dir == "DOWN":
        print(f"  ⚠️ Orderbook BULLISH but momentum DOWN — conflicting signals, skipping")
        return
    if ob_bias == "BEARISH" and momentum_dir == "UP":
        print(f"  ⚠️ Orderbook BEARISH but momentum UP — conflicting signals, skipping")
        return

    # 6. Entry price from orderbook
    if side == "yes":
        entry_cents = ob_signal.get("yes_ask_cents", 50)
    else:
        entry_cents = ob_signal.get("no_ask_cents", 50)
    entry_price = entry_cents / 100

    # 7. Position size — capped at 3 contracts with $10 balance
    position_dollars = risk.calculate_position_size(
        balance, conviction=momentum["confidence"],
        win_rate=0.58, avg_win=0.80, avg_loss=1.0,
    )
    contracts_count = max(1, min(3, int(position_dollars)))

    # 8. Place order
    try:
        order = await client.place_order(
            ticker=ticker, action="buy", side=side, count=contracts_count,
            yes_price=entry_cents if side == "yes" else None,
            no_price=entry_cents if side == "no" else None,
        )
        print(f"  [ORDER RESPONSE] {order}")

        if order.get("error") or not order.get("order"):
            print(f"  ❌ Order rejected: {order}")
            return

        order_status = order.get("order", {}).get("status", "unknown")
        order_id = order.get("order", {}).get("order_id") or order.get("order", {}).get("id")
        filled = order_status in ("filled", "resting", "pending")
        _last_trade_time = time.time()
        _daily_trade_count += 1
        print(f"  📊 Daily trades: {_daily_trade_count}/{MAX_DAILY_TRADES}")

        trade_data = {
            **momentum,
            "order_id": order_id,
            "ob_imbalance": ob_signal.get("imbalance"),
            "ob_bias": ob_bias,
        }
        log_trade(
            bot_id=BOT_ID,
            ticker=ticker,
            side=side.upper(),
            quantity=contracts_count,
            entry_price=entry_price,
            strategy="BTC Directional",
            status="FILLED" if filled else order_status.upper(),
            order_type="LIMIT",
            sentiment_score=momentum["confidence"],
            reasoning=(
                f"BTC {momentum['direction']} {momentum['change_pct']:+.4f}% | "
                f"Conf: {momentum['confidence']:.2f} | OB bias: {ob_bias} "
                f"({ob_signal.get('imbalance', 0.5):.2f})"
            ),
            metadata=trade_data,
        )

        print(f"  ✅ {side.upper()} {contracts_count}x {ticker} @ {entry_cents}¢ "
              f"| BTC {momentum['direction']} {momentum['change_pct']:+.4f}% "
              f"| OB: {ob_bias} ({ob_signal.get('imbalance', 0.5):.2f})")

        # 9. Spawn position monitor as background task (mid-window exit)
        if filled:
            # Get the DB trade id we just inserted
            res = supabase.table("trades").select("id").eq("bot_id", BOT_ID) \
                .order("opened_at", desc=True).limit(1).execute()
            trade_db_id = res.data[0]["id"] if res.data else None
            if trade_db_id:
                asyncio.create_task(
                    monitor_and_exit(client, ticker, side, contracts_count,
                                     entry_cents, trade_db_id)
                )

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

    # Start live Binance WebSocket feed as background task
    feed = BinanceLiveFeed()
    feed.start()
    print("   Waiting 5s for WebSocket to seed price history...")
    await asyncio.sleep(5)

    # Verify Kalshi connection and write initial balance
    try:
        balance = await client.get_balance()
        print(f"   Balance: ${balance:,.2f}")
        update_pnl(BOT_ID, balance)
    except Exception as e:
        import traceback
        print(f"   Auth error: {e}\n{traceback.format_exc()}")
        balance = 0

    # Auto-resume if paused
    state = get_bot_state(BOT_ID)
    if not state.get("active"):
        print(f"   Bot was paused — resuming on startup")
        update_bot_state(BOT_ID, active=True, mode="AGGRESSIVE")

    print(f"   High-activity windows (UTC): {HIGH_ACTIVITY_WINDOWS_UTC}")
    print(f"   Profit target: +{PROFIT_TARGET:.0%} | Stop loss: -{STOP_LOSS:.0%}")

    while True:
        state = get_bot_state(BOT_ID)
        if not state.get("active"):
            print(f"[{BOT_ID}] Paused. Sleeping 60s...")
            await asyncio.sleep(60)
            continue

        update_bot_state(BOT_ID, last_heartbeat=datetime.now(timezone.utc).isoformat())

        risk_check = risk.check_limits()
        if risk_check["action"] != "OK":
            print(f"[{BOT_ID}] Risk: {risk_check['reason']}")
            await asyncio.sleep(300)
            continue

        try:
            balance = await client.get_balance()
            update_pnl(BOT_ID, balance)

            await settle_trades(client, adaptive)
            params = adaptive.load()

            # Get momentum from live WebSocket feed using learned lookback
            lookback_s = params["lookback_minutes"] * 60
            momentum = feed.get_momentum(lookback_seconds=lookback_s)
            btc_price = momentum.get("current_price", 0)

            in_window = is_high_activity_hour()
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] "
                  f"BTC: ${btc_price:,.2f} | {momentum['direction']} "
                  f"({momentum['change_pct']:+.4f}%) | Conf: {momentum['confidence']:.2f} "
                  f"| Threshold: {params['confidence_threshold']:.2f} "
                  f"| Window: {'✅' if in_window else '⏸'}")

            if (momentum["confidence"] >= params["confidence_threshold"]
                    and momentum["direction"] != "NEUTRAL"
                    and abs(momentum["change_pct"]) >= MIN_CHANGE_PCT):
                # Get orderbook signal for the best available contract
                contracts = await find_5min_btc_contracts(client)
                if contracts:
                    ob_signal = await get_orderbook_signal(client, contracts[0].get("ticker", ""))
                    print(f"  OB imbalance: {ob_signal['imbalance']:.2f} ({ob_signal['bias']}) "
                          f"| YES: {ob_signal.get('yes_vol', 0)} / NO: {ob_signal.get('no_vol', 0)}")
                    await execute_trade(client, risk, momentum, ob_signal, balance)
                else:
                    print("  No contracts found for orderbook check")
            elif momentum["direction"] == "NEUTRAL":
                print(f"  Momentum NEUTRAL — waiting")
            elif abs(momentum["change_pct"]) < MIN_CHANGE_PCT:
                print(f"  BTC move {momentum['change_pct']:+.4f}% below min {MIN_CHANGE_PCT}% — waiting")
            else:
                print(f"  Below confidence threshold ({momentum['confidence']:.2f} < {params['confidence_threshold']:.2f}) — waiting")

        except Exception as e:
            import traceback
            print(f"  Error: {e}\n{traceback.format_exc()}")

        # Run every 60 seconds (was 5 min) — cooldown handles entry rate limiting
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
