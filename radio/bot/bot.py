"""AshXMusic bot — polling thread + identity setup + live NP panel.

Modular layout (this is the orchestrator):
  api.py       Telegram Bot API client
  keyboards.py inline buttons (play/pause/skip/loop/shuffle/autoplay)
  panel.py     live now-playing message with progress tracker
  handlers.py  command + callback implementations
  configure.py /configure — RTMP creds + workflow restart
"""
import os
import threading

from radio.bot.api import TelegramAPI, env_token, env_control
from radio.bot.handlers import Handlers
from radio.bot.panel import NowPlayingPanel


class TgBot(threading.Thread):
    def __init__(self, station):
        super().__init__(daemon=True)
        self.station = station
        # radio/bot/bot.py -> repo root
        self.repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        self.api = TelegramAPI(env_token())
        self.control = env_control()
        self.panel = NowPlayingPanel(self, self.control)
        self.h = Handlers(self)
        self._stop = False
        if self.api.ok and self.control:
            station.log(f"tgbot: control chat {self.control}")
        else:
            station.log("tgbot: disabled (need TG_BOT_TOKEN + TG_CHAT_ID)")

    # ---------- helpers used by handlers/panel ----------

    def kb_np(self, paused: bool | None = None) -> dict:
        from radio.bot import keyboards
        st = self.station
        if paused is None:
            paused = bool(getattr(st, "pause_ev", None) and
                          st.pause_ev.is_set())
        return keyboards.np_kb(
            paused=paused,
            loop=st.state.get("loop", "off"),
            autoplay=st.state.get("autoplay", True),
            shuffle=st.state.get("shuffle", False))

    def reply(self, chat_id, text: str):
        self.api.send_message(chat_id, text)

    def notify_nowplaying(self, entry: dict, cover: str = ""):
        """Called by the station when a new song goes on air."""
        if not (self.api.ok and self.control):
            return
        try:
            self.panel.start(entry, cover_path=cover or "")
        except Exception as e:
            station_log = getattr(self.station, "log", print)
            station_log(f"np panel: {e}")

    def stop_polling(self):
        self._stop = True

    # ---------- thread ----------

    def run(self):
        if not (self.api.ok and self.control):
            return
        # bot identity: rename + about + command menu (new API surface)
        self.api.setup_identity(commands=[
            {"command": "play", "description": "Play a song next"},
            {"command": "add", "description": "Add a song to the queue"},
            {"command": "search", "description": "Search and pick a song"},
            {"command": "np", "description": "Now playing + controls"},
            {"command": "pause", "description": "Pause playback"},
            {"command": "resume", "description": "Resume playback"},
            {"command": "skip", "description": "Skip current song"},
            {"command": "queue", "description": "Show the queue"},
            {"command": "autoplay", "description": "Autoplay on/off"},
            {"command": "loop", "description": "Loop off/one/all"},
            {"command": "shuffle", "description": "Shuffle on/off"},
            {"command": "volume", "description": "Volume 0-150"},
            {"command": "status", "description": "Stream status"},
            {"command": "stats", "description": "Play stats"},
            {"command": "configure", "description": "Set RTMP server + key"},
            {"command": "config", "description": "Show stream config"},
            {"command": "stopstream", "description": "Stop the radio"},
            {"command": "help", "description": "Full guide"},
        ])
        # drop updates sent before this run (stale chat must not queue songs)
        try:
            self.api.poll(timeout=0, drop_stale=True)
        except Exception:
            pass
        while not self._stop:
            try:
                ups = self.api.poll(timeout=50)
            except Exception:
                continue
            for upd in ups:
                try:
                    if upd.get("message"):
                        self.h.handle_message(upd["message"])
                    elif upd.get("callback_query"):
                        cq = upd["callback_query"]
                        self.h.handle_callback(cq)
                except Exception:
                    pass
