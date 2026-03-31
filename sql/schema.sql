-- ============================================================
-- RYAN'S TRADING HQ — SUPABASE SCHEMA
-- Run this in Supabase SQL Editor (supabase.com/dashboard)
-- ============================================================

-- 1. BOT STATE — tracks each bot's current mode and status
CREATE TABLE IF NOT EXISTS bot_state (
  id TEXT PRIMARY KEY,                    -- 'alpaca', 'kalshi_btc', 'kalshi_arb'
  active BOOLEAN DEFAULT true,
  mode TEXT DEFAULT 'AGGRESSIVE',         -- AGGRESSIVE, CAUTIOUS, DEFENSIVE, KILLED
  last_heartbeat TIMESTAMPTZ,
  daily_pnl NUMERIC DEFAULT 0,
  weekly_pnl NUMERIC DEFAULT 0,
  monthly_pnl NUMERIC DEFAULT 0,
  high_water_mark NUMERIC DEFAULT 0,
  current_drawdown_pct NUMERIC DEFAULT 0,
  metadata JSONB DEFAULT '{}',
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Seed bot state rows
INSERT INTO bot_state (id, active, mode) VALUES
  ('alpaca', true, 'AGGRESSIVE'),
  ('kalshi_btc', true, 'AGGRESSIVE'),
  ('kalshi_arb', true, 'AGGRESSIVE')
ON CONFLICT (id) DO NOTHING;

-- 2. TRADES — every trade across all bots
CREATE TABLE IF NOT EXISTS trades (
  id BIGSERIAL PRIMARY KEY,
  bot_id TEXT NOT NULL REFERENCES bot_state(id),
  ticker TEXT,                            -- 'AAPL', 'BTC 15m Up/Down', etc.
  side TEXT NOT NULL,                     -- 'BUY', 'SELL', 'YES', 'NO'
  quantity NUMERIC,
  entry_price NUMERIC,
  exit_price NUMERIC,
  pnl NUMERIC,
  strategy TEXT,                          -- 'Momentum', 'Latency Arb', 'News-Driven', etc.
  status TEXT DEFAULT 'PENDING',          -- PENDING, FILLED, CANCELLED, CLOSED
  order_type TEXT,                        -- 'MARKET', 'LIMIT', 'BRACKET'
  stop_loss NUMERIC,
  take_profit NUMERIC,
  edge_pct NUMERIC,                       -- for arb trades: detected edge %
  latency_ms INTEGER,                     -- execution latency
  sentiment_score NUMERIC,                -- from Perplexity research
  reasoning TEXT,                         -- AI's reasoning for the trade
  metadata JSONB DEFAULT '{}',
  opened_at TIMESTAMPTZ DEFAULT now(),
  closed_at TIMESTAMPTZ
);

CREATE INDEX idx_trades_bot ON trades(bot_id);
CREATE INDEX idx_trades_opened ON trades(opened_at DESC);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_trades_ticker ON trades(ticker);

-- 3. HOLDINGS — current Alpaca portfolio positions
CREATE TABLE IF NOT EXISTS holdings (
  id BIGSERIAL PRIMARY KEY,
  ticker TEXT NOT NULL UNIQUE,
  name TEXT,
  shares NUMERIC NOT NULL DEFAULT 0,
  avg_cost NUMERIC,
  current_price NUMERIC,
  market_value NUMERIC,
  unrealized_pnl NUMERIC,
  pnl_pct NUMERIC,
  sector TEXT,
  last_updated TIMESTAMPTZ DEFAULT now()
);

-- 4. EQUITY CURVE — daily snapshots for each bot
CREATE TABLE IF NOT EXISTS equity_curve (
  id BIGSERIAL PRIMARY KEY,
  bot_id TEXT NOT NULL REFERENCES bot_state(id),
  value NUMERIC NOT NULL,
  daily_return_pct NUMERIC,
  cumulative_pnl NUMERIC,
  trade_count INTEGER DEFAULT 0,
  win_count INTEGER DEFAULT 0,
  recorded_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_equity_bot_date ON equity_curve(bot_id, recorded_at DESC);

-- 5. RESEARCH CACHE — Perplexity query results
CREATE TABLE IF NOT EXISTS research_cache (
  id BIGSERIAL PRIMARY KEY,
  query TEXT NOT NULL,
  summary TEXT,
  sentiment TEXT,                          -- 'bullish', 'bearish', 'neutral'
  sentiment_score NUMERIC,                 -- -1.0 to 1.0
  confidence NUMERIC,
  catalysts TEXT[],
  risks TEXT[],
  relevant_bots TEXT[],                    -- ['alpaca', 'kalshi_btc', 'kalshi_arb']
  raw_response JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_research_created ON research_cache(created_at DESC);
CREATE INDEX idx_research_sentiment ON research_cache(sentiment);

-- 6. SIGNALS — technical + sentiment signals for watchlist tickers
CREATE TABLE IF NOT EXISTS signals (
  id BIGSERIAL PRIMARY KEY,
  ticker TEXT NOT NULL,
  signal_type TEXT NOT NULL,               -- 'TECHNICAL', 'SENTIMENT', 'REGIME'
  direction TEXT,                          -- 'BULLISH', 'BEARISH', 'NEUTRAL'
  strength NUMERIC,                        -- 0-1 scale
  details JSONB,                           -- RSI, MACD, EMA values etc.
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_signals_ticker ON signals(ticker, created_at DESC);

-- 7. WATCHLIST — dynamic ticker watchlist
CREATE TABLE IF NOT EXISTS watchlist (
  ticker TEXT PRIMARY KEY,
  name TEXT,
  added_reason TEXT,
  mention_count INTEGER DEFAULT 1,
  last_signal_at TIMESTAMPTZ,
  added_at TIMESTAMPTZ DEFAULT now()
);

-- 8. ALERTS — telegram notifications log
CREATE TABLE IF NOT EXISTS alerts (
  id BIGSERIAL PRIMARY KEY,
  bot_id TEXT REFERENCES bot_state(id),
  alert_type TEXT NOT NULL,                -- 'TRADE', 'KILL_SWITCH', 'DAILY_PNL', 'DRAWDOWN'
  message TEXT NOT NULL,
  sent BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 9. ARB METRICS — Kalshi arb-specific monitoring
CREATE TABLE IF NOT EXISTS arb_metrics (
  id BIGSERIAL PRIMARY KEY,
  measured_lag_s NUMERIC,                  -- measured Kalshi lag in seconds
  edge_pct NUMERIC,                        -- detected edge percentage
  btc_binance_price NUMERIC,
  btc_kalshi_implied NUMERIC,
  spread NUMERIC,
  action_taken TEXT,                        -- 'TRADED', 'SKIPPED', 'BELOW_THRESHOLD'
  recorded_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_arb_metrics_date ON arb_metrics(recorded_at DESC);

-- ============================================================
-- REALTIME — enable for dashboard live updates
-- ============================================================
ALTER PUBLICATION supabase_realtime ADD TABLE trades;
ALTER PUBLICATION supabase_realtime ADD TABLE equity_curve;
ALTER PUBLICATION supabase_realtime ADD TABLE bot_state;
ALTER PUBLICATION supabase_realtime ADD TABLE research_cache;
ALTER PUBLICATION supabase_realtime ADD TABLE arb_metrics;
ALTER PUBLICATION supabase_realtime ADD TABLE holdings;

-- ============================================================
-- RLS (Row Level Security) — keep it simple, service role only
-- ============================================================
ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE holdings ENABLE ROW LEVEL SECURITY;
ALTER TABLE equity_curve ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE watchlist ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE arb_metrics ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (bots use service key)
CREATE POLICY "Service role full access" ON bot_state FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON trades FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON holdings FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON equity_curve FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON research_cache FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON signals FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON watchlist FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON alerts FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON arb_metrics FOR ALL USING (true) WITH CHECK (true);
