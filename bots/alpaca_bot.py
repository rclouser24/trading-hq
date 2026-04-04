"""
ALPACA BOT — Aggressive Momentum Swing Trader
Hold: 2-14 days | Entry: Triple confirmation (technicals + sentiment + regime)
Position sizing: 8-15% half-Kelly | Max 6 concurrent positions
"""
import os
import sys
import asyncio
import time
from datetime import datetime, timezone, timedelta

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.shared import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    supabase, log_trade, update_trade, log_equity, log_signal,
    get_bot_state, update_bot_state, send_alert,
    RiskManager, query_perplexity, ask_claude,
)

BOT_ID = "alpaca"

# ─── ALPACA API CLIENT ────────────────────────────────────────────
class AlpacaClient:
    def __init__(self):
        self.base_url = ALPACA_BASE_URL
        self.data_url = "https://data.alpaca.markets"
        self.headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }

    async def get_account(self) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base_url}/v2/account", headers=self.headers, timeout=10)
            return r.json()

    async def get_positions(self) -> list:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base_url}/v2/positions", headers=self.headers, timeout=10)
            return r.json()

    async def get_bars(self, ticker: str, timeframe: str = "1Day", limit: int = 60) -> list:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.data_url}/v2/stocks/{ticker}/bars",
                headers=self.headers,
                params={"timeframe": timeframe, "limit": limit, "feed": "iex"},
                timeout=10,
            )
            return r.json().get("bars", [])

    async def submit_order(self, ticker: str, qty: float, side: str,
                           order_type: str = "market", limit_price: float = None,
                           take_profit: float = None, stop_loss: float = None) -> dict:
        order = {
            "symbol": ticker,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": "day",
        }
        if limit_price and order_type == "limit":
            order["limit_price"] = str(limit_price)

        # Bracket order
        if take_profit and stop_loss:
            order["order_class"] = "bracket"
            order["take_profit"] = {"limit_price": str(take_profit)}
            order["stop_loss"] = {"stop_price": str(stop_loss)}

        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.base_url}/v2/orders",
                headers=self.headers,
                json=order,
                timeout=10,
            )
            return r.json()

    async def close_position(self, ticker: str) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{self.base_url}/v2/positions/{ticker}",
                headers=self.headers,
                timeout=10,
            )
            return r.json()

    async def close_all_positions(self) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{self.base_url}/v2/positions",
                headers=self.headers,
                timeout=10,
            )
            return r.json()

    async def is_market_open(self) -> bool:
        """Check Alpaca clock API — respects weekends and holidays."""
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base_url}/v2/clock", headers=self.headers, timeout=10)
            return r.json().get("is_open", False)


# ─── TECHNICAL ANALYSIS ───────────────────────────────────────────
def calculate_ema(prices: list, period: int) -> list:
    """Calculate EMA from price list."""
    if len(prices) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def calculate_rsi(prices: list, period: int = 14) -> float:
    """Calculate RSI."""
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(prices: list) -> dict:
    """Calculate MACD (12, 26, 9)."""
    if len(prices) < 26:
        return {"macd": 0, "signal": 0, "histogram": 0}
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
    signal_line = calculate_ema(macd_line, 9) if len(macd_line) >= 9 else [0]
    histogram = macd_line[-1] - signal_line[-1] if signal_line else 0
    return {
        "macd": macd_line[-1] if macd_line else 0,
        "signal": signal_line[-1] if signal_line else 0,
        "histogram": histogram,
    }


def analyze_technicals(bars: list) -> dict:
    """Run full technical analysis on price bars."""
    if len(bars) < 50:
        return {"score": 0, "signals": {}, "pass": False}

    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    current_price = closes[-1]

    # EMAs
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema20_val = ema20[-1] if ema20 else 0
    ema50_val = ema50[-1] if ema50 else 0

    # RSI
    rsi = calculate_rsi(closes)

    # MACD
    macd = calculate_macd(closes)

    # Volume
    avg_volume_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
    current_volume = volumes[-1]
    volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 1

    # Scoring
    signals = {
        "price_above_ema20": current_price > ema20_val,
        "ema20_above_ema50": ema20_val > ema50_val,
        "rsi_in_range": 50 <= rsi <= 75,
        "macd_positive": macd["macd"] > macd["signal"] and macd["histogram"] > 0,
        "volume_surge": volume_ratio >= 1.5,
    }

    score = sum(signals.values()) / len(signals)
    all_pass = all(signals.values())

    return {
        "score": score,
        "signals": signals,
        "pass": all_pass,
        "rsi": round(rsi, 2),
        "macd": macd,
        "ema20": round(ema20_val, 2),
        "ema50": round(ema50_val, 2),
        "volume_ratio": round(volume_ratio, 2),
        "current_price": current_price,
    }


# ─── MARKET REGIME CHECK ─────────────────────────────────────────
async def check_market_regime(client: AlpacaClient) -> dict:
    """Check S&P 500 trend + VIX level."""
    spy_bars = await client.get_bars("SPY", limit=60)
    if not spy_bars:
        return {"pass": False, "reason": "No SPY data"}

    spy_closes = [b["c"] for b in spy_bars]
    spy_ema50 = calculate_ema(spy_closes, 50)
    spy_above_ema = spy_closes[-1] > spy_ema50[-1] if spy_ema50 else False

    # VIX check — use VIXY as proxy (VIX not directly available on Alpaca)
    # In production, fetch VIX from a data provider
    vix_ok = True  # Default to OK, override with actual VIX data

    return {
        "pass": spy_above_ema and vix_ok,
        "spy_price": spy_closes[-1],
        "spy_ema50": round(spy_ema50[-1], 2) if spy_ema50 else 0,
        "spy_above_ema": spy_above_ema,
        "vix_ok": vix_ok,
    }


# ─── MAIN TRADING LOOP ───────────────────────────────────────────
WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

async def run_scan_cycle(client: AlpacaClient, risk: RiskManager):
    """One complete scan cycle: check regime, scan watchlist, execute entries."""
    state = get_bot_state(BOT_ID)
    if not state.get("active"):
        print(f"[{BOT_ID}] Bot is paused. Skipping cycle.")
        return

    # Heartbeat
    update_bot_state(BOT_ID, last_heartbeat=datetime.now(timezone.utc).isoformat())

    # Risk check
    risk_check = risk.check_limits()
    if risk_check["action"] != "OK":
        print(f"[{BOT_ID}] Risk limit: {risk_check['reason']}")
        return

    # Market regime
    regime = await check_market_regime(client)
    log_signal("SPY", "REGIME", "BULLISH" if regime["pass"] else "BEARISH",
               1.0 if regime["pass"] else 0.0, regime)

    if not regime["pass"]:
        print(f"[{BOT_ID}] Market regime BEARISH — skipping entries. SPY: {regime.get('spy_price')}")
        if state.get("mode") != "DEFENSIVE":
            update_bot_state(BOT_ID, mode="DEFENSIVE")
            send_alert(BOT_ID, "REGIME_SHIFT", "Market regime turned bearish. Switching to DEFENSIVE mode.")
        return
    elif state.get("mode") == "DEFENSIVE":
        update_bot_state(BOT_ID, mode="AGGRESSIVE")
        send_alert(BOT_ID, "REGIME_SHIFT", "Market regime bullish again. Back to AGGRESSIVE.")

    # Get current positions
    positions = await client.get_positions()
    held_tickers = [p["symbol"] for p in positions]
    open_count = len(positions)

    if open_count >= 6:
        print(f"[{BOT_ID}] Max positions reached ({open_count}/6). Managing existing only.")
        return

    # Get account for sizing
    account = await client.get_account()
    portfolio_value = float(account.get("portfolio_value", 0))
    buying_power = float(account.get("buying_power", 0))

    # Scan watchlist
    for ticker in WATCHLIST:
        if ticker in held_tickers:
            continue
        if open_count >= 6:
            break

        try:
            # 1. Technical analysis
            bars = await client.get_bars(ticker, limit=60)
            technicals = analyze_technicals(bars)
            log_signal(ticker, "TECHNICAL",
                       "BULLISH" if technicals["pass"] else "NEUTRAL",
                       technicals["score"], technicals)

            if not technicals["pass"]:
                print(f"  [{ticker}] Technicals FAIL (score: {technicals['score']:.2f})")
                continue

            # 2. Perplexity sentiment
            research = await query_perplexity(f"{ticker} stock sentiment news catalyst this week")
            log_signal(ticker, "SENTIMENT",
                       research["sentiment"].upper(),
                       (research["score"] + 1) / 2, {"summary": research["summary"]})

            if research["score"] < 0.4:
                print(f"  [{ticker}] Sentiment below threshold ({research['score']:.2f})")
                continue

            # 3. All signals aligned — calculate position size
            conviction = (technicals["score"] + (research["score"] + 1) / 2) / 2
            position_dollars = risk.calculate_position_size(portfolio_value, conviction)
            current_price = technicals["current_price"]
            shares = int(position_dollars / current_price)

            if shares < 1 or position_dollars > buying_power:
                print(f"  [{ticker}] Insufficient buying power")
                continue

            # 4. Calculate bracket levels
            take_profit = round(current_price * 1.12, 2)   # +12%
            stop_loss = round(current_price * 0.94, 2)      # -6%

            # 5. Ask Claude for final reasoning
            reasoning = await ask_claude(
                "You are an aggressive momentum swing trading bot. Confirm or reject this trade in 1-2 sentences.",
                f"ENTRY SIGNAL for {ticker}: Price ${current_price}, RSI {technicals['rsi']}, "
                f"MACD histogram {technicals['macd']['histogram']:.4f}, Volume {technicals['volume_ratio']:.1f}x avg, "
                f"Sentiment: {research['sentiment']} ({research['score']:.2f}). "
                f"Position: {shares} shares (${position_dollars:.0f}, {(position_dollars/portfolio_value*100):.1f}% of portfolio). "
                f"TP: ${take_profit}, SL: ${stop_loss}."
            )

            # 6. Execute bracket order
            print(f"  [{ticker}] ✅ ALL SIGNALS ALIGNED — Executing BUY {shares} @ ~${current_price}")
            print(f"    Reasoning: {reasoning}")

            order = await client.submit_order(
                ticker=ticker,
                qty=shares,
                side="buy",
                order_type="market",
                take_profit=take_profit,
                stop_loss=stop_loss,
            )

            # 7. Log trade
            log_trade(
                bot_id=BOT_ID,
                ticker=ticker,
                side="BUY",
                quantity=shares,
                entry_price=current_price,
                strategy="Momentum",
                status="FILLED" if order.get("status") == "accepted" else "PENDING",
                order_type="BRACKET",
                stop_loss=stop_loss,
                take_profit=take_profit,
                sentiment_score=research["score"],
                reasoning=reasoning,
                metadata={"technicals": technicals, "sentiment": research["sentiment"]},
            )

            send_alert(BOT_ID, "TRADE",
                       f"🟢 BUY {shares}x {ticker} @ ${current_price}\n"
                       f"TP: ${take_profit} (+12%) | SL: ${stop_loss} (-6%)\n"
                       f"RSI: {technicals['rsi']} | Sentiment: {research['score']:.2f}\n"
                       f"Size: ${position_dollars:.0f} ({(position_dollars/portfolio_value*100):.1f}%)")

            open_count += 1
            await asyncio.sleep(1)  # Rate limiting

        except Exception as e:
            print(f"  [{ticker}] Error: {e}")
            continue


async def manage_positions(client: AlpacaClient):
    """Check existing positions for exit signals (time stop, sentiment reversal)."""
    positions = await client.get_positions()

    for pos in positions:
        ticker = pos["symbol"]
        entry_price = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])
        unrealized_pnl_pct = float(pos["unrealized_plpc"]) * 100
        qty = float(pos["qty"])

        # Time stop: check if position is older than 5 days with < +3% gain
        # (Would check trade opened_at from DB in production)

        # Sentiment reversal check
        try:
            research = await query_perplexity(f"{ticker} stock sentiment news today")
            if research["score"] < -0.2:
                print(f"  [{ticker}] SENTIMENT REVERSAL ({research['score']:.2f}) — Exiting")
                await client.close_position(ticker)
                send_alert(BOT_ID, "EXIT",
                           f"🔴 SENTIMENT EXIT {ticker} @ ${current_price} "
                           f"(P&L: {unrealized_pnl_pct:+.1f}%) — Score dropped to {research['score']:.2f}")
        except Exception as e:
            print(f"  [{ticker}] Sentiment check error: {e}")

        await asyncio.sleep(0.5)


# ─── MAIN LOOP ────────────────────────────────────────────────────
async def main():
    print(f"🚀 Alpaca Momentum Bot starting...")
    print(f"   Base URL: {ALPACA_BASE_URL}")
    print(f"   Watchlist: {', '.join(WATCHLIST)}")

    client = AlpacaClient()
    risk = RiskManager(BOT_ID, daily_limit_pct=-3.0, weekly_limit_pct=-8.0, monthly_kill_pct=-15.0)

    # Verify connection
    account = await client.get_account()
    print(f"   Account: {account.get('account_number', 'N/A')}")
    print(f"   Portfolio: ${float(account.get('portfolio_value', 0)):,.2f}")
    print(f"   Buying Power: ${float(account.get('buying_power', 0)):,.2f}")
    print(f"   Paper: {'paper' in ALPACA_BASE_URL}")
    print()

    while True:
        now = datetime.now(timezone.utc)

        market_open = await client.is_market_open()

        if market_open:
            print(f"\n[{now.strftime('%H:%M:%S')}] Market open — running scan cycle...")
            try:
                account = await client.get_account()
                portfolio_value = float(account.get("portfolio_value", 0))
                update_bot_state(BOT_ID, metadata={"portfolio_balance": round(portfolio_value, 2)})
                await run_scan_cycle(client, risk)
                await manage_positions(client)
            except Exception as e:
                print(f"Cycle error: {e}")
                send_alert(BOT_ID, "ERROR", f"Scan cycle error: {e}")
            await asyncio.sleep(900)  # 15 min during market hours
        else:
            print(f"[{now.strftime('%H:%M:%S')}] Market closed. Sleeping 60 min...")
            await asyncio.sleep(3600)  # 60 min when closed


if __name__ == "__main__":
    asyncio.run(main())
