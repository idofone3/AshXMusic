"""Live Now-Playing panel — one Telegram message kept up to date.

A daemon thread edits the NP message every ~8s with a text progress
tracker (▰▱ bar + clock), so listeners see playback move in the chat,
plus the full ⏸ ▶ ⏭ 🔁 🔀 button pad.
"""
import threading
import time


def bar(frac: float, width: int = 18) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(frac * width)
    return "▰" * filled + "▱" * (width - filled)


def clock(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class NowPlayingPanel:
    def __init__(self, bot, chat_id: str):
        self.bot = bot
        self.chat = str(chat_id)
        self.msg_id = None
        self.is_photo = False
        self._stop = threading.Event()
        self._thread = None

    # ---------- lifecycle ----------

    def start(self, entry: dict, cover_path: str = ""):
        self.stop()
        e = entry or {}
        el = self.bot.station.elapsed() if self.bot.station else 0
        dur = float(e.get("dur") or 0)
        cap = self._caption(e, el, dur)
        kb = self.bot.kb_np()
        r = {"ok": False}
        if cover_path:
            r = self.bot.api.send_photo(self.chat, cover_path, caption=cap,
                                        kb=kb)
        if not r.get("ok"):
            r = self.bot.api.send_message(self.chat, cap, kb=kb)
        if r.get("ok"):
            m = r.get("result") or {}
            self.msg_id = m.get("message_id")
            self.is_photo = bool(m.get("photo"))
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self.msg_id = None

    def refresh_now(self):
        """Force one immediate edit (used after pause/resume/skip)."""
        self._edit_once()

    # ---------- internals ----------

    def _caption(self, e: dict, el: float, dur: float) -> str:
        st = self.bot.station
        paused = bool(st and getattr(st, "pause_ev", None) and
                      st.pause_ev.is_set())
        state = "⏸ <b>PAUSED</b>" if paused else "▶️ <b>Now playing</b>"
        title = e.get("title", "Unknown")
        artist = e.get("artist", "Unknown")
        by = e.get("by")
        req = f"\n🎙 requested by: <i>{by}</i>" if by else ""
        tline = ""
        if dur > 0:
            tline = (f"\n<code>{bar(el / dur)}</code>\n"
                     f"<code>{clock(el)}</code> / <code>{clock(dur)}</code>"
                     f"  •  {int(el / dur * 100) if dur else 0}%")
        nxt = ""
        try:
            q = st.queue_snapshot(1) if st else []
            if q:
                nxt = f"\n⏭ up next: <b>{q[0].get('title', '?')}</b>"
        except Exception:
            pass
        return (f"{state}\n🎵 <b>{title}</b>\n👤 {artist}{req}{tline}{nxt}\n"
                f"📻 AshXMusic • 24/7 YouTube Music radio")

    def _edit_once(self):
        if not self.msg_id:
            return
        st = self.bot.station
        e = st.current_entry() if st else None
        if not e:
            return
        el = st.elapsed() if st else 0
        dur = float(e.get("dur") or 0)
        cap = self._caption(e, el, dur)
        kb = self.bot.kb_np(paused=st.pause_ev.is_set() if st else False)
        if self.is_photo:
            self.bot.api.edit_caption(self.chat, self.msg_id, caption=cap,
                                      kb=kb)
        else:
            self.bot.api.edit_message(self.chat, self.msg_id, text=cap, kb=kb)

    def _loop(self):
        self._stop.clear()
        # small initial delay so the first edit shows progress moved
        delay = 8.0
        while not self._stop.wait(delay):
            try:
                self._edit_once()
            except Exception:
                pass
