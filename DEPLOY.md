# DEPLOYMENT GUIDE — Ryan's Trading HQ

## Quick Start (30 minutes to live)

### Step 1: Supabase Schema (5 min)
1. Go to your Supabase dashboard → SQL Editor
2. Paste the entire contents of `sql/schema.sql`
3. Click "Run" — this creates all 9 tables + indexes + realtime

### Step 2: Create GitHub Repo (2 min)
1. Create a new private repo: `trading-hq`
2. Push this entire folder to it
3. Make sure `.env.example` is committed but NOT actual keys

### Step 3: Railway Setup (10 min)
1. Go to railway.app → New Project → Deploy from GitHub Repo
2. Select your `trading-hq` repo
3. Railway will detect the `railway.toml` and `requirements.txt`
4. Go to Settings → Variables and add ALL env vars from `.env.example`
   - Use your real Supabase URL + service key
   - Use Alpaca PAPER keys first (https://paper-api.alpaca.markets)
   - Add Kalshi key ID + full private key (with \n for newlines)
   - Add Perplexity API key
   - Add Anthropic API key
   - (Optional) Add Telegram bot token + chat ID
5. Deploy — Railway will build and start all 4 services

### Step 4: Verify Bots Are Running (5 min)
1. Check Railway logs — you should see:
   - `🚀 Alpaca Momentum Bot starting...`
   - `🚀 Kalshi BTC Directional Bot starting...`
   - `🚀 Kalshi Latency Arb Bot starting...`
   - `🔍 Perplexity Research Service starting...`
2. Check Supabase — the `bot_state` table should show heartbeats updating
3. Check `research_cache` — should start filling with Perplexity results

### Step 5: Dashboard (5 min)
1. Push the React dashboard to a separate repo (or same repo, different branch)
2. Deploy to Vercel: vercel.com → Import Project → select repo
3. Add env vars in Vercel:
   - `NEXT_PUBLIC_SUPABASE_URL`
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
4. The dashboard will read from Supabase realtime subscriptions

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Railway ($5/mo)                    │
│                                                       │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │  Alpaca Bot   │  │ Kalshi BTC   │                 │
│  │  (15m cycle)  │  │ (5m cycle)   │                 │
│  └──────┬───────┘  └──────┬───────┘                 │
│         │                  │                          │
│  ┌──────┴───────┐  ┌──────┴───────┐                 │
│  │  Kalshi Arb   │  │  Research    │                 │
│  │  (5s cycle)   │  │  (5m cycle)  │                 │
│  └──────┬───────┘  └──────┬───────┘                 │
│         │                  │                          │
│         └────────┬─────────┘                         │
│                  ▼                                    │
│           ┌────────────┐                             │
│           │  Supabase   │◄── Realtime subscriptions  │
│           └──────┬─────┘                             │
│                  │                                    │
└──────────────────┼──────────────────────────────────┘
                   ▼
          ┌────────────────┐
          │ Vercel Dashboard │  ← React app (free tier)
          └────────────────┘
```

## Bot Schedules

| Bot | Cycle | When | What it does |
|-----|-------|------|-------------|
| Alpaca | 15 min | Market hours only (9:30-4 ET) | Scans watchlist, checks technicals + sentiment, executes bracket orders |
| Kalshi BTC | 5 min | 24/7 | Monitors BTC momentum, trades directional contracts |
| Kalshi Arb | 5 sec | 24/7 | Monitors Binance vs Kalshi lag, executes arb trades |
| Research | 5 min | 24/7 | Polls Perplexity on schedule, caches results |

## Going Live (Paper → Real Money)

1. Run on paper for at least 1-2 weeks
2. Check Supabase `trades` table — verify win rates and P&L
3. When ready, change `ALPACA_BASE_URL` to `https://api.alpaca.markets`
4. Fund your Kalshi account with starting capital
5. Start with minimum position sizes and scale gradually

## Kill Switch

If anything goes wrong:
- Toggle `active` to `false` in the `bot_state` table → bots stop immediately
- Or set `mode` to `KILLED` → bots liquidate and stop
- Railway dashboard → click "Stop" on the service
- Telegram alerts will fire automatically at drawdown thresholds
