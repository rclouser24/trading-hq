"""
Trading HQ — Shared Config & Utilities
All bots import from here for DB, alerts, and common helpers.
"""
import os
import json
import time
import hmac
import hashlib
import base64
import httpx
from datetime import datetime, timezone
from supabase import create_client, Client

# ─── ENV VARS ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
# Normalize PEM key: handle literal \n and reformat continuous base64 body
def _normalize_pem(raw: str) -> str:
    if not raw:
        return raw
    raw = raw.replace("\\n", "\n").strip()
    # Extract header, body, footer
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    header = lines[0]   # -----BEGIN RSA PRIVATE KEY-----
    footer = lines[-1]  # -----END RSA PRIVATE KEY-----
    body = "".join(lines[1:-1])  # join all middle lines (handles both formats)
    # Re-wrap body at 64 chars per line as PEM requires
    wrapped = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n"

KALSHI_PRIVATE_KEY = _normalize_pem(os.environ.get("KALSHI_PRIVATE_KEY", ""))

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # paper by default

PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── SUPABASE CLIENT ──────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ─── DATABASE HELPERS ─────────────────────────────────────────────
def log_trade(bot_id: str, **kwargs):
    """Log a trade to the trades table."""
    data = {"bot_id": bot_id, **kwargs, "opened_at": datetime.now(timezone.utc).isoformat()}
    supabase.table("trades").insert(data).execute()


def update_trade(trade_id: int, **kwargs):
    """Update an existing trade."""
    supabase.table("trades").update(kwargs).eq("id", trade_id).execute()


def log_equity(bot_id: str, value: float, daily_return_pct: float = 0, 
               cumulative_pnl: float = 0, trade_count: int = 0, win_count: int = 0):
    """Log daily equity snapshot."""
    supabase.table("equity_curve").insert({
        "bot_id": bot_id,
        "value": value,
        "daily_return_pct": daily_return_pct,
        "cumulative_pnl": cumulative_pnl,
        "trade_count": trade_count,
        "win_count": win_count,
    }).execute()


def get_bot_state(bot_id: str) -> dict:
    """Get current bot state."""
    res = supabase.table("bot_state").select("*").eq("id", bot_id).single().execute()
    return res.data


def update_bot_state(bot_id: str, **kwargs):
    """Update bot state."""
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    kwargs["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
    supabase.table("bot_state").update(kwargs).eq("id", bot_id).execute()


def log_research(query: str, summary: str, sentiment: str, sentiment_score: float,
                 confidence: float = 0, catalysts: list = None, risks: list = None,
                 relevant_bots: list = None, raw_response: dict = None):
    """Cache a Perplexity research result."""
    supabase.table("research_cache").insert({
        "query": query,
        "summary": summary,
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "confidence": confidence,
        "catalysts": catalysts or [],
        "risks": risks or [],
        "relevant_bots": relevant_bots or [],
        "raw_response": raw_response or {},
    }).execute()


def log_arb_metric(lag_s: float, edge_pct: float, btc_binance: float,
                   btc_kalshi_implied: float, spread: float, action: str):
    """Log arb monitoring data."""
    supabase.table("arb_metrics").insert({
        "measured_lag_s": lag_s,
        "edge_pct": edge_pct,
        "btc_binance_price": btc_binance,
        "btc_kalshi_implied": btc_kalshi_implied,
        "spread": spread,
        "action_taken": action,
    }).execute()


def log_signal(ticker: str, signal_type: str, direction: str, strength: float, details: dict = None):
    """Log a technical/sentiment signal."""
    supabase.table("signals").insert({
        "ticker": ticker,
        "signal_type": signal_type,
        "direction": direction,
        "strength": strength,
        "details": details or {},
    }).execute()


# ─── TELEGRAM ALERTS ──────────────────────────────────────────────
def send_alert(bot_id: str, alert_type: str, message: str):
    """Send a Telegram alert and log it."""
    # Log to DB
    supabase.table("alerts").insert({
        "bot_id": bot_id,
        "alert_type": alert_type,
        "message": message,
        "sent": bool(TELEGRAM_BOT_TOKEN),
    }).execute()

    # Send via Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            httpx.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"🤖 [{bot_id.upper()}] {alert_type}\n\n{message}",
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            print(f"Telegram alert failed: {e}")


# ─── RISK MANAGEMENT ─────────────────────────────────────────────
class RiskManager:
    """Shared risk management across all bots."""

    def __init__(self, bot_id: str, daily_limit_pct: float = -3.0,
                 weekly_limit_pct: float = -8.0, monthly_kill_pct: float = -15.0):
        self.bot_id = bot_id
        self.daily_limit = daily_limit_pct
        self.weekly_limit = weekly_limit_pct
        self.monthly_kill = monthly_kill_pct

    def check_limits(self) -> dict:
        """Check if any risk limits are breached. Returns action to take."""
        state = get_bot_state(self.bot_id)
        daily = state.get("daily_pnl", 0)
        weekly = state.get("weekly_pnl", 0)
        monthly = state.get("monthly_pnl", 0)
        hwm = state.get("high_water_mark", 0)

        if hwm > 0:
            drawdown = ((daily - hwm) / hwm) * 100 if hwm else 0
        else:
            drawdown = 0

        # Monthly kill switch
        if monthly <= self.monthly_kill:
            send_alert(self.bot_id, "KILL_SWITCH",
                       f"🚨 MONTHLY DRAWDOWN {monthly:.1f}% — KILLING BOT. All positions will be liquidated.")
            update_bot_state(self.bot_id, active=False, mode="KILLED")
            return {"action": "KILL", "reason": f"Monthly drawdown {monthly:.1f}%"}

        # Weekly halt
        if weekly <= self.weekly_limit:
            send_alert(self.bot_id, "WEEKLY_HALT",
                       f"⚠️ Weekly loss {weekly:.1f}% — halting new entries.")
            update_bot_state(self.bot_id, mode="DEFENSIVE")
            return {"action": "HALT", "reason": f"Weekly loss {weekly:.1f}%"}

        # Daily halt
        if daily <= self.daily_limit:
            send_alert(self.bot_id, "DAILY_HALT",
                       f"⚠️ Daily loss {daily:.1f}% — no new trades today.")
            return {"action": "PAUSE_DAY", "reason": f"Daily loss {daily:.1f}%"}

        return {"action": "OK", "reason": "Within limits"}

    def calculate_position_size(self, portfolio_value: float, conviction: float = 0.5,
                                win_rate: float = 0.6, avg_win: float = 0.12,
                                avg_loss: float = 0.06) -> float:
        """Half-Kelly position sizing."""
        if avg_loss == 0:
            return portfolio_value * 0.03  # minimum

        kelly = (win_rate * (avg_win / avg_loss) - (1 - win_rate)) / (avg_win / avg_loss)
        half_kelly = max(kelly * 0.5, 0.03)  # floor at 3%
        half_kelly = min(half_kelly, 0.15)     # cap at 15%

        # Scale by conviction
        size_pct = half_kelly * conviction
        size_pct = max(size_pct, 0.03)
        size_pct = min(size_pct, 0.15)

        return portfolio_value * size_pct


# ─── PERPLEXITY RESEARCH ─────────────────────────────────────────
async def query_perplexity(query: str) -> dict:
    """Query Perplexity API for real-time research."""
    if not PERPLEXITY_API_KEY:
        return {"summary": "Perplexity API key not configured", "sentiment": "neutral", "score": 0}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "sonar",
                "messages": [
                    {"role": "system", "content": "You are a financial research assistant. Provide concise, actionable market intelligence. End every response with a sentiment tag in this exact format on its own line: [sentiment: bullish/bearish/neutral | score: -1.0 to 1.0 | confidence: 0-1]"},
                    {"role": "user", "content": query},
                ],
            },
            timeout=30,
        )
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Parse sentiment from response
        sentiment = "neutral"
        score = 0.0
        confidence = 0.5
        import re
        match = re.search(r'\[sentiment:\s*(bullish|bearish|neutral)\s*\|\s*score:\s*([-\d.]+)\s*\|\s*confidence:\s*([\d.]+)\]', content, re.I)
        if match:
            sentiment = match.group(1).lower()
            score = float(match.group(2))
            confidence = float(match.group(3))

        summary = re.sub(r'\[sentiment:.*?\]', '', content).strip()

        return {
            "summary": summary,
            "sentiment": sentiment,
            "score": score,
            "confidence": confidence,
            "raw": content,
        }


# ─── CLAUDE AI REASONING ─────────────────────────────────────────
async def ask_claude(system_prompt: str, user_message: str) -> str:
    """Ask Claude for strategy reasoning."""
    if not ANTHROPIC_API_KEY:
        return "Claude API key not configured"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=30,
        )
        data = resp.json()
        return data.get("content", [{}])[0].get("text", "No response")
