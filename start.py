"""
Entry point — picks the right bot based on Railway service name.
"""
import os
import subprocess
import sys

service = os.environ.get("RAILWAY_SERVICE_NAME", "").lower()

commands = {
    "kalshi_btc": ["python", "bots/kalshi_btc_bot.py"],
    "kalshi_arb": ["python", "bots/kalshi_arb_bot.py"],
    "alpaca":     ["python", "bots/alpaca_bot.py"],
    "research":   ["python", "bots/research_service.py"],
}

cmd = commands.get(service)
if not cmd:
    print(f"Unknown service name: '{service}'. Expected one of: {list(commands.keys())}")
    sys.exit(1)

print(f"Starting service: {service} → {' '.join(cmd)}")
os.execvp(cmd[0], cmd)
