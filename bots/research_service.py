"""
PERPLEXITY RESEARCH SERVICE — Auto-polling research feed
Runs scheduled queries and caches results for all bots + dashboard.
"""
import os
import sys
import asyncio
import httpx
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.shared import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    supabase, log_research, query_perplexity, get_bot_state,
)

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

async def is_market_open() -> bool:
    """Check Alpaca clock — respects weekends and holidays."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{ALPACA_BASE_URL}/v2/clock", headers=ALPACA_HEADERS, timeout=10)
            return r.json().get("is_open", False)
    except Exception:
        return False

# ─── SCHEDULED QUERIES ────────────────────────────────────────────
SCHEDULES = [
    {
        "query": "Bitcoin BTC price outlook macro sentiment this week crypto market",
        "interval_minutes": 30,
        "relevant_bots": ["kalshi_btc", "kalshi_arb"],
        "last_run": 0,
    },
    {
        "query": "S&P 500 market regime VIX volatility outlook today",
        "interval_minutes": 120,
        "relevant_bots": ["alpaca"],
        "last_run": 0,
    },
    {
        "query": "Federal Reserve interest rate decision economy macro outlook",
        "interval_minutes": 240,
        "relevant_bots": ["alpaca", "kalshi_btc", "kalshi_arb"],
        "last_run": 0,
    },
    {
        "query": "Kalshi prediction market crypto contracts pricing efficiency latency",
        "interval_minutes": 360,
        "relevant_bots": ["kalshi_arb"],
        "last_run": 0,
    },
]

# Holdings-specific queries (generated dynamically)
WATCHLIST_TICKERS = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL", "AMD"]
TICKER_INTERVAL_MINUTES = 60


# ─── MAIN LOOP ────────────────────────────────────────────────────
async def run_scheduled_query(schedule: dict):
    """Execute a single scheduled research query."""
    query = schedule["query"]
    print(f"  🔍 Querying: {query[:60]}...")

    try:
        result = await query_perplexity(query)

        log_research(
            query=query,
            summary=result["summary"],
            sentiment=result["sentiment"],
            sentiment_score=result["score"],
            confidence=result["confidence"],
            relevant_bots=schedule["relevant_bots"],
            raw_response={"raw": result.get("raw", "")},
        )

        print(f"     → {result['sentiment']} ({result['score']:+.2f}) | "
              f"Confidence: {result['confidence']:.2f}")

    except Exception as e:
        print(f"     → Error: {e}")


async def run_ticker_query(ticker: str):
    """Run sentiment query for a specific ticker."""
    query = f"{ticker} stock sentiment news catalyst earnings outlook this week"
    print(f"  📊 Ticker scan: {ticker}")

    try:
        result = await query_perplexity(query)

        log_research(
            query=query,
            summary=result["summary"],
            sentiment=result["sentiment"],
            sentiment_score=result["score"],
            confidence=result["confidence"],
            relevant_bots=["alpaca"],
            raw_response={"ticker": ticker, "raw": result.get("raw", "")},
        )

        print(f"     → {ticker}: {result['sentiment']} ({result['score']:+.2f})")

    except Exception as e:
        print(f"     → {ticker} error: {e}")


async def main():
    print(f"🔍 Perplexity Research Service starting...")
    print(f"   Scheduled queries: {len(SCHEDULES)}")
    print(f"   Watchlist tickers: {', '.join(WATCHLIST_TICKERS)}")
    print(f"   Ticker poll interval: {TICKER_INTERVAL_MINUTES}m")
    print()

    last_ticker_run = 0

    while True:
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        market_open = await is_market_open()
        print(f"\n[{now.strftime('%H:%M:%S')}] Research cycle... (market {'open' if market_open else 'closed'})")

        # Crypto queries run 24/7 regardless of market hours
        for schedule in SCHEDULES:
            is_crypto_query = any(b in schedule["relevant_bots"] for b in ["kalshi_btc", "kalshi_arb"])
            is_stock_query = "alpaca" in schedule["relevant_bots"] and not is_crypto_query

            # Skip stock-only queries when market is closed
            if is_stock_query and not market_open:
                continue

            elapsed = (now_ts - schedule["last_run"]) / 60
            if elapsed >= schedule["interval_minutes"]:
                await run_scheduled_query(schedule)
                schedule["last_run"] = now_ts
                await asyncio.sleep(2)  # Rate limiting

        # Ticker scans only when market is open
        if market_open:
            ticker_elapsed = (now_ts - last_ticker_run) / 60
            if ticker_elapsed >= TICKER_INTERVAL_MINUTES:
                print(f"\n  Running watchlist scan...")
                for ticker in WATCHLIST_TICKERS:
                    await run_ticker_query(ticker)
                    await asyncio.sleep(3)  # Rate limiting between tickers
                last_ticker_run = now_ts
        else:
            print(f"  Skipping stock ticker scans (market closed).")

        # Sleep 5 minutes between cycles
        print(f"\n  Next cycle in 5 minutes...")
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(main())
