"""Standalone Telegram bot runner (the bot also auto-starts with run.py).

Run:  python3 bot.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ytm.engine import Engine
from ytm.telegram_bot import get_bot

BASE = os.path.dirname(os.path.abspath(__file__))
engine = Engine(os.path.join(BASE, "cookies.txt"),
                os.path.join(BASE, "downloads"))

bot = get_bot(BASE, engine)
if not bot.start():
    print("bot not configured — set bot token + chat id first "
          "(data/telegram.json or POST /telegram)")
    sys.exit(1)

print("bot running — Ctrl+C to stop")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    bot.stop()
    print("stopped")
