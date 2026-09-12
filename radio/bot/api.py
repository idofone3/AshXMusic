"""Raw Telegram Bot API client — no frameworks, pure requests.

Covers everything the radio needs, including the newer API surface:
  setMyName / setMyDescription / setMyShortDescription (bot identity),
  setMyCommands (menu button), link_preview_options, sendRichMessage with
  layered fallbacks (Bot API 10.x), editMessageText / editMessageCaption,
  answerCallbackQuery, sendChatAction.
"""
import os
import threading

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramAPI:
    def __init__(self, token: str):
        self.token = (token or "").strip()
        self._offset = 0
        self._lock = threading.Lock()

    @property
    def ok(self) -> bool:
        return bool(self.token)

    # ---------- core ----------

    def call(self, method: str, timeout: int = 40, **params) -> dict:
        if not self.token:
            return {"ok": False, "description": "no token"}
        import requests
        try:
            r = requests.post(_API.format(token=self.token, method=method),
                              json=params, timeout=timeout)
            return r.json() or {"ok": False,
                                "description": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "description": str(e)[:200]}

    def call_multipart(self, method: str, file_field: str, path: str,
                       **params) -> dict:
        import requests
        try:
            with open(path, "rb") as f:
                r = requests.post(_API.format(token=self.token, method=method),
                                  data=params, files={file_field: f},
                                  timeout=90)
            return r.json() or {"ok": False}
        except Exception as e:
            return {"ok": False, "description": str(e)[:200]}

    # ---------- identity (rename the bot etc.) ----------

    def setup_identity(self, name="AshXMusic 🎵",
                       description="24/7 Telegram radio on a YouTube Music "
                                   "queue. Send any song name and it plays "
                                   "next — with cover art, blur background "
                                   "and a live progress tracker.",
                       short="24/7 YouTube Music radio — request songs by name",
                       commands=None):
        self.call("setMyName", name=name)
        self.call("setMyDescription", description=description)
        self.call("setMyShortDescription", short_description=short)
        if commands:
            self.call("setMyCommands", commands=commands)

    # ---------- messaging ----------

    def send_message(self, chat_id, text: str, kb: dict | None = None,
                     rich_html: str | None = None) -> dict:
        """rich (Bot API 10.x) -> classic HTML -> plain text."""
        if rich_html:
            r = self.call("sendRichMessage", chat_id=chat_id,
                          rich_message={"html": rich_html})
            if r.get("ok"):
                return r
        body = {"chat_id": chat_id, "text": (rich_html or text)[:4000],
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True}}
        if kb:
            body["reply_markup"] = kb
        r = self.call("sendMessage", **body)
        if r.get("ok"):
            return r
        body.pop("parse_mode", None)
        body.pop("reply_markup", None)
        body["text"] = _strip_html(body["text"])
        return self.call("sendMessage", **body)

    def edit_message(self, chat_id, message_id, text: str,
                     kb: dict | None = None) -> dict:
        body = {"chat_id": chat_id, "message_id": message_id,
                "text": text[:4000], "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True}}
        if kb:
            body["reply_markup"] = kb
        r = self.call("editMessageText", **body)
        if not r.get("ok") and "not modified" in str(r.get("description", "")):
            r["ok"] = True  # same content — treat as success
        return r

    def send_photo(self, chat_id, path: str, caption: str = "",
                   kb: dict | None = None) -> dict:
        params = {"chat_id": chat_id, "caption": caption[:1024],
                  "parse_mode": "HTML"}
        if kb:
            params["reply_markup"] = kb
        r = self.call_multipart("sendPhoto", "photo", path, **params)
        if not r.get("ok"):
            return self.send_message(chat_id, caption, kb=kb)
        return r

    def edit_caption(self, chat_id, message_id, caption: str,
                     kb: dict | None = None) -> dict:
        body = {"chat_id": chat_id, "message_id": message_id,
                "caption": caption[:1024], "parse_mode": "HTML"}
        if kb:
            body["reply_markup"] = kb
        r = self.call("editMessageCaption", **body)
        if not r.get("ok") and "not modified" in str(r.get("description", "")):
            r["ok"] = True
        return r

    def delete_message(self, chat_id, message_id) -> dict:
        return self.call("deleteMessage", chat_id=chat_id,
                         message_id=message_id)

    def chat_action(self, chat_id, action="typing") -> dict:
        return self.call("sendChatAction", chat_id=chat_id, action=action)

    def answer_callback(self, callback_id, text: str = "",
                        alert: bool = False) -> dict:
        p = {"callback_query_id": callback_id}
        if text:
            p["text"] = text[:190]
        p["show_alert"] = alert
        return self.call("answerCallbackQuery", **p)

    # ---------- polling ----------

    def poll(self, timeout: int = 50, drop_stale: bool = False) -> list:
        with self._lock:
            off = -1 if drop_stale else self._offset
            data = self.call("getUpdates", offset=off,
                             timeout=max(0, timeout),
                             allowed_updates=["message", "callback_query"])
            ups = data.get("result") or []
            if ups:
                self._offset = ups[-1]["update_id"] + 1
            return ups


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return (s or "").strip()


def env_token() -> str:
    return os.environ.get("TG_BOT_TOKEN", "").strip()


def env_control() -> str:
    return str(os.environ.get("TG_CHAT_ID", "") or "").strip()
