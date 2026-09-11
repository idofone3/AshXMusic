"""Telegram control bot — the radio's remote control.

Any plain text message from the control chat is a song request
(plays next, YT-Music style). Commands:
  /play <song>   search + play next (also the default for plain text)
  /add <song>    append to the end of the queue
  /skip          cut the current song
  /np            now playing
  /queue         show the queue
  /help          commands
"""
import os
import threading
import time

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, **params):
    import requests
    try:
        r = requests.post(_API.format(token=token, method=method),
                          json=params, timeout=70)
        return r.json() or {}
    except Exception:
        return {}


class TgBot(threading.Thread):
    def __init__(self, station):
        super().__init__(daemon=True)
        self.station = station
        self.token = os.environ.get("TG_BOT_TOKEN", "")
        self.control = str(os.environ.get("TG_CHAT_ID", "") or "")
        self._offset = 0
        self._stop = False
        if self.token and self.control:
            self.station.log(f"tgbot: control chat {self.control}")

    # ---------- helpers ----------

    def reply(self, chat_id, text: str):
        if not (self.token and chat_id):
            return
        _call(self.token, "sendMessage", chat_id=chat_id, text=text[:4000],
              parse_mode="HTML",
              link_preview_options={"is_disabled": True})

    def notify_nowplaying(self, entry: dict, cover: str):
        if not (self.token and self.control):
            return
        cap = (f"▶️ <b>Now playing</b>\n"
               f"🎵 <b>{entry.get('title', 'Unknown')}</b>\n"
               f"👤 {entry.get('artist', 'Unknown')}\n"
               f"📻 Hindi Hits Radio • 24/7")
        params = {"chat_id": self.control, "caption": cap[:1000],
                  "parse_mode": "HTML"}
        try:
            if cover and os.path.exists(cover):
                with open(cover, "rb") as f:
                    import requests
                    requests.post(
                        _API.format(token=self.token, method="sendPhoto"),
                        data=params, files={"photo": f}, timeout=60)
            else:
                _call(self.token, "sendMessage", chat_id=self.control,
                      text=cap, parse_mode="HTML",
                      link_preview_options={"is_disabled": True})
        except Exception:
            pass

    def stop_polling(self):
        self._stop = True

    # ---------- command handling ----------

    def _handle(self, msg: dict):
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # only the control chat can steer the radio
        if self.control and chat != self.control:
            return
        low = text.lower()
        if low.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower().strip()
            arg = arg.strip()
            if cmd == "/help" or cmd == "/start":
                self.reply(chat, "📻 <b>Hindi Hits Radio</b>\n"
                                 "/play &lt;song&gt; — play next\n"
                                 "/add &lt;song&gt; — add to queue\n"
                                 "/skip — cut current song\n"
                                 "/np — now playing\n"
                                 "/queue — show queue\n\n"
                                 "Or just send any song name as a message.")
            elif cmd == "/play":
                self._request(chat, arg, front=True)
            elif cmd == "/add":
                self._request(chat, arg, front=False)
            elif cmd == "/skip":
                self.station.request_skip()
                self.reply(chat, "⏭️ Skipping…")
            elif cmd == "/np":
                e = self.station.current_entry()
                if e:
                    el = self.station.elapsed()
                    self.reply(chat, f"▶️ <b>{e.get('title')}</b> — "
                                     f"{e.get('artist')}  "
                                     f"[{int(el)//60}:{int(el)%60:02d} / "
                                     f"{e.get('dur_str', '?')}]")
                else:
                    self.reply(chat, "💤 Idle — nothing playing")
            elif cmd == "/queue":
                q = self.station.queue_snapshot(8)
                if not q:
                    self.reply(chat, "Queue is empty (radio picks next)")
                    return
                lines = [f"<b>Up next</b>"]
                for i, e in enumerate(q, 1):
                    tag = "⭐" if e.get("by") else "•"
                    lines.append(f"{i}. {tag} {e.get('title', '?')} — "
                                 f"{e.get('artist', '?')}")
                self.reply(chat, "\n".join(lines))
            return
        # plain text = song request (plays next)
        self._request(chat, text, front=True)

    def _request(self, chat: str, query: str, front: bool):
        if not query:
            self.reply(chat, "Usage: /play <song name>")
            return
        ok = self.station.request_song(query, front=front)
        if ok:
            pos = "top of the queue" if front else "the queue"
            self.reply(chat, f"🎵 <b>{query}</b> added to {pos} — "
                             f"searching & playing…")
        else:
            self.reply(chat, "⏳ Another request is still being searched, "
                             "try again in a moment.")

    # ---------- long poll ----------

    def run(self):
        if not (self.token and self.control):
            return
        # drop everything sent before this run (stale group chatter must
        # not storm the queue as song requests)
        try:
            data = _call(self.token, "getUpdates", offset=-1, timeout=0)
            ups = data.get("result") or []
            if ups:
                self._offset = ups[-1]["update_id"] + 1
        except Exception:
            pass
        while not self._stop:
            data = _call(self.token, "getUpdates", offset=self._offset,
                         timeout=50, allowed_updates=["message"])
            for upd in (data.get("result") or []):
                self._offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                try:
                    self._handle(msg)
                except Exception:
                    pass
