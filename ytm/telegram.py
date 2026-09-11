"""
Telegram log: every finished song is pushed to a Telegram chat as an mp4
with cover art. Configure via POST /telegram {bot_token, chat_id} or env
vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Send strategy:
  1. sendAudio  -> renders as a playable music widget w/ cover + title
  2. sendDocument -> fallback that always delivers the raw .mp4 file
"""
import json
import os
import threading
from typing import Dict, Optional

import requests

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramLogger:
    def __init__(self, base_dir: str):
        self._path = os.path.join(base_dir, "data", "telegram.json")
        self._lock = threading.Lock()
        self._cfg: Dict = {"bot_token": "", "chat_id": "", "enabled": True}
        self._load()
        # env fallback (only fills empty fields)
        if not self._cfg.get("bot_token"):
            self._cfg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self._cfg.get("chat_id"):
            self._cfg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "")

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    self._cfg.update(json.load(f))
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, indent=2)
        except Exception:
            pass

    # ------------- config -------------

    def set(self, bot_token: str, chat_id: str,
            enabled: Optional[bool] = None) -> Dict:
        with self._lock:
            self._cfg["bot_token"] = (bot_token or "").strip()
            self._cfg["chat_id"] = str(chat_id or "").strip()
            if enabled is not None:
                self._cfg["enabled"] = bool(enabled)
            self._save()
            return self.status()

    def status(self) -> Dict:
        tok = self._cfg.get("bot_token") or ""
        return {
            "configured": bool(tok and self._cfg.get("chat_id")),
            "enabled": bool(self._cfg.get("enabled", True)),
            "bot_token": (tok[:8] + "…" + tok[-4:]) if len(tok) > 14 else tok,
            "chat_id": self._cfg.get("chat_id", ""),
        }

    def active(self) -> bool:
        st = self.status()
        return st["configured"] and st["enabled"]

    def active_for(self, chat_id: Optional[str] = None) -> bool:
        """Token present + enabled, target = override or configured chat."""
        if not self._cfg.get("bot_token") or not self._cfg.get("enabled", True):
            return False
        return bool(chat_id or self._cfg.get("chat_id"))

    # ------------- sending -------------

    def _call(self, token: str, method: str, timeout: int = 30, **kw) -> Dict:
        r = requests.post(_API.format(token=token, method=method),
                          timeout=timeout, **kw)
        try:
            data = r.json()
        except Exception:
            data = {"ok": False, "description": f"HTTP {r.status_code}"}
        return data

    def send_test(self, bot_token: Optional[str] = None,
                  chat_id: Optional[str] = None) -> Dict:
        tok = bot_token or self._cfg.get("bot_token") or ""
        chat = chat_id or self._cfg.get("chat_id") or ""
        if not tok or not chat:
            return {"ok": False, "description": "bot_token / chat_id missing"}
        return self._call(tok, "sendMessage", json={
            "chat_id": chat,
            "text": "ytm-api connected — finished songs will land here as mp4",
        })

    def send_song(self, mp4_path: str, title: str = "", artist: str = "",
                  thumb: Optional[str] = None,
                  duration: int = 0, chat_id: Optional[str] = None) -> Dict:
        """Push the finished mp4. sendAudio first (music widget + cover),
        then sendDocument fallback. chat_id overrides the configured chat
        (used by the /get bot so the song lands where it was requested).
        Returns a small report dict."""
        if not self.active_for(str(chat_id) if chat_id else None):
            return {"sent": False, "reason": "telegram not configured/enabled"}
        tok = self._cfg["bot_token"]
        chat = str(chat_id) if chat_id else self._cfg["chat_id"]
        caption = f"{artist} — {title}".strip(" —") or "new song"

        # telegram thumbs must be JPEG <=200kB; ours already is (~30-80kB)
        files = {"audio": open(mp4_path, "rb")}
        try:
            data = {
                "chat_id": chat,
                "title": (title or "Unknown")[:60],
                "performer": (artist or "Unknown")[:60],
                "caption": caption[:1000],
            }
            if duration:
                data["duration"] = int(duration)
            if thumb and os.path.exists(thumb) and \
                    os.path.getsize(thumb) < 200_000:
                files["thumb"] = open(thumb, "rb")
                try:
                    r = self._call(tok, "sendAudio", timeout=120,
                                   data=data, files=files)
                    if r.get("ok"):
                        return {"sent": True, "via": "sendAudio"}
                except Exception:
                    pass
            r = self._call(tok, "sendDocument", timeout=120, data={
                "chat_id": chat, "caption": caption[:1000]},
                files={"document": open(mp4_path, "rb")})
            if r.get("ok"):
                return {"sent": True, "via": "sendDocument"}
            return {"sent": False, "error": r.get("description", "?")}
        finally:
            for f in files.values():
                try:
                    f.close()
                except Exception:
                    pass


_singleton: Optional[TelegramLogger] = None
_once = threading.Lock()


def get_logger(base_dir: str) -> TelegramLogger:
    global _singleton
    with _once:
        if _singleton is None:
            _singleton = TelegramLogger(base_dir)
    return _singleton
