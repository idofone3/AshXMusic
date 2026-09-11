"""Background Telegram bot for ytm-api (Bot API 10.x "Rich Messages").

Runs as a daemon thread next to the FastAPI server (auto-started on boot)
or standalone:  python3 bot.py

Commands (group or private):
  /get <song name>   search YT Music -> rich results (heading, bordered
                     table, inline LaTeX footer, per-result callback
                     buttons); in groups results are sent as an EPHEMERAL
                     message visible only to the requester
  /status            last downloads as a rich table
  /help              rich help with a LaTeX block demo
  1..5               text alias for the numbered result buttons

Tap a numbered button -> download starts -> live progress edits -> the
finished MP4 (cover art embedded) lands right in the chat.

Protocol notes (checked against core.telegram.org/bots/api, Aug 2026):
  * sendRichMessage(chat_id, rich_message=InputRichMessage{html|markdown|blocks})
  * InputRichMessage.html supports <h1-6> <table bordered striped compact>
    <details> <tg-math>INLINE LATEX</tg-math> <tg-math-block>BLOCK LATEX</tg-math-block>
    <tg-button type="callback_data" style="primary" data="...">label</tg-button>
    <blockquote expandable> <footer> <hr/> ...
  * EphemeralMessageParameters{receiver_user_id} on sendRichMessage ->
    message visible only to that user (10.2/10.3)
  * Every send falls back to classic sendMessage HTML, and finally to
    plain text, so the bot degrades gracefully on older API versions.
"""
from __future__ import annotations

import html as _html
import json
import re
import threading
import time
import traceback
from typing import Dict, List, Optional

import requests

from . import telegram as ytg
from .engine import Engine
from .telegram import get_logger as tg_logger

_API = "https://api.telegram.org/bot{token}/{method}"

_BADWORDS = ("remix", "lofi", "slowed", "reverb", "cover", "instrumental",
             "karaoke", "nightcore", "8d", "sped up", "reprise", "acoustic",
             "encore", "unplugged", "live")


def _esc(s: str) -> str:
    return _html.escape(str(s or ""), quote=False)


def _fmt_dur(sec) -> str:
    try:
        sec = int(sec)
        return f"{sec // 60}:{sec % 60:02d}"
    except Exception:
        return "–"


def _strip_html(s: str) -> str:
    s = re.sub(r"<tg-button[^>]*>.*?</tg-button>", "", s, flags=re.S)
    s = re.sub(r"<(h\d|p|li|tr|footer|figcaption|summary)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(s)).strip()


class YTMusicBot:
    def __init__(self, base_dir: str, engine: Engine):
        self.base_dir = base_dir
        self.engine = engine
        self.tg = tg_logger(base_dir)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._offset = 0
        self._lock = threading.Lock()
        self._pending: Dict[str, List[Dict]] = {}   # chat_key -> results
        self._inflight: Dict[str, bool] = {}        # videoId -> downloading

    # ---------- config ----------
    def _token_raw(self) -> str:
        cfg = self.tg._cfg.get("bot_token") or ""
        return cfg.strip()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> Dict:
        return {"running": self.running,
                "configured": self.tg.active(),
                "bot": self.tg.status()}

    # ---------- lifecycle ----------
    def start(self):
        if self.running or not self._token_raw() or not self.tg.active():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="tg-bot-poll")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _loop(self):
        tok = self._token_raw()
        log = lambda *a: print("[tg-bot]", *a, flush=True)
        log(f"polling started (chat {self.tg._cfg.get('chat_id')})")
        # drop stale updates so an old /get doesn't replay
        try:
            requests.post(_API.format(token=tok, method="getUpdates"),
                          json={"offset": -1, "timeout": 0}, timeout=10)
        except Exception:
            pass
        backoff = 1
        while not self._stop.is_set():
            try:
                r = requests.post(_API.format(token=tok, method="getUpdates"),
                                  json={"offset": self._offset, "timeout": 25,
                                        "allowed_updates": ["message",
                                                            "callback_query"]},
                                  timeout=35)
                data = r.json()
                if not data.get("ok"):
                    desc = data.get("description", "")
                    if "Conflict" in desc:      # another poller running
                        log("409 conflict — another poller? sleeping 10s")
                        time.sleep(10)
                        continue
                    if "401" in desc or "Unauthorized" in desc:
                        log("bad token:", desc)
                        time.sleep(30)
                        continue
                    time.sleep(backoff)
                    continue
                backoff = 1
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    try:
                        self._handle_update(upd)
                    except Exception:
                        log("update error:\n" + traceback.format_exc()[-600:])
            except requests.RequestException as e:
                time.sleep(min(backoff, 15))
                backoff = min(backoff * 2, 15)
            except Exception:
                log("loop error:\n" + traceback.format_exc()[-600:])
                time.sleep(5)
        log("polling stopped")

    # ---------- updates ----------
    def _handle_update(self, upd: Dict):
        if upd.get("callback_query"):
            self._on_callback(upd["callback_query"])
            return
        msg = upd.get("message")
        if not msg or not (msg.get("text") or "").strip():
            return
        txt = msg["text"].strip()
        chat = msg["chat"]
        chat_id = chat["id"]
        frm = (msg.get("from") or {}).get("id")
        mid = msg["message_id"]
        # /command@BotName or /command
        m = re.match(r"^/(get|status|help|start|id)(?:@\w+)?\s*(.*)$",
                     txt, re.I | re.S)
        if m:
            cmd, arg = m.group(1).lower(), m.group(2).strip()
            if cmd == "get":
                self._cmd_get(chat_id, mid, frm, arg)
            elif cmd == "status":
                self._cmd_status(chat_id, mid)
            elif cmd == "id":
                self._send(chat_id, f"<p>chat id: <code>{chat_id}</code>, "
                                    f"your id: <code>{frm}</code></p>")
            else:
                self._cmd_help(chat_id, mid)
            return
        # "1".."5" picks from the last results in this chat
        key = str(chat_id)
        if key in self._pending and re.fullmatch(r"[1-5]", txt):
            res = self._pending[key]
            idx = int(txt) - 1
            if idx < len(res):
                self._start_download(chat_id, res[idx])

    # ---------- sends (rich with layered fallbacks) ----------
    def _api(self, token: str, method: str, timeout: int = 40, **kw) -> Dict:
        r = requests.post(_API.format(token=token, method=method),
                          timeout=timeout, **kw)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "description": f"HTTP {r.status_code}"}

    def _send(self, chat_id: int, rich_html: str, reply_to: int = None,
              ephemeral_user: int = None, simple_html: str = None,
              keyboard: Optional[Dict] = None) -> Dict:
        """sendRichMessage with graceful degradation:
        rich html -> classic sendMessage html -> plain text."""
        tok = self._token_raw()
        if not tok:
            return {"ok": False, "description": "no token"}
        body: Dict = {"chat_id": chat_id,
                      "rich_message": {"html": rich_html}}
        if reply_to:
            body["reply_parameters"] = {"message_id": reply_to,
                                        "allow_sending_without_reply": True}
        if ephemeral_user:
            body["ephemeral_message_parameters"] = {
                "receiver_user_id": ephemeral_user}
        r = self._api(tok, "sendRichMessage", json=body)
        if r.get("ok"):
            return r
        # fallback 1: classic HTML message
        text = simple_html or rich_html
        if len(text) > 4096:
            text = text[:4000] + " …"
        body2: Dict = {"chat_id": chat_id, "text": text,
                       "parse_mode": "HTML",
                       "disable_web_page_preview": True}
        if reply_to:
            body2["reply_parameters"] = {"message_id": reply_to,
                                         "allow_sending_without_reply": True}
        if keyboard:
            body2["reply_markup"] = keyboard
        r = self._api(tok, "sendMessage", json=body2)
        if r.get("ok"):
            return r
        # fallback 2: plain text
        body2.pop("parse_mode")
        body2["text"] = _strip_html(text)
        return self._api(tok, "sendMessage", json=body2)

    def _edit(self, chat_id: int, message_id: int, rich_html: str,
              simple_html: str = None) -> Dict:
        tok = self._token_raw()
        r = self._api(tok, "editMessageText", json={
            "chat_id": chat_id, "message_id": message_id,
            "rich_message": {"html": rich_html}})
        if r.get("ok"):
            return r
        r = self._api(tok, "editMessageText", json={
            "chat_id": chat_id, "message_id": message_id,
            "text": simple_html or _strip_html(rich_html),
            "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.get("ok"):
            return r
        return self._api(tok, "editMessageText", json={
            "chat_id": chat_id, "message_id": message_id,
            "text": _strip_html(simple_html or rich_html)})

    # ---------- commands ----------
    def _search(self, query: str) -> List[Dict]:
        res = self.engine.search(query, flt="songs", limit=10)
        out = []
        for r in res:
            t = (r.get("title") or "").lower()
            if any(w in t for w in _BADWORDS):
                continue
            out.append(r)
            if len(out) >= 5:
                break
        return out or res[:5]

    def _cmd_get(self, chat_id: int, mid: int, user_id: int, query: str):
        if not query:
            self._send(chat_id, "<p>Usage: <code>/get &lt;song name&gt;</code>"
                                "</p>", reply_to=mid)
            return
        ephemeral = None
        is_group = chat_id < 0
        try:
            res = self._search(query)
        except Exception as e:
            self._send(chat_id, f"<p>search failed: <code>{_esc(str(e)[:150])}"
                                f"</code></p>", reply_to=mid)
            return
        if not res:
            self._send(chat_id, f"<p>No results for “{_esc(query)}”.</p>",
                       reply_to=mid)
            return
        self._pending[str(chat_id)] = res

        rows = []
        for i, r in enumerate(res, 1):
            rows.append(
                f"<tr><td align=\"center\">{i}</td>"
                f"<td><b>{_esc(r.get('title') or '')}</b></td>"
                f"<td>{_esc(r.get('artist') or '')}</td>"
                f"<td align=\"center\"><code>{_esc(r.get('duration') or '–')}</code></td></tr>")
        buttons = " ".join(
            f"<tg-button type=\"callback_data\" style=\"primary\" "
            f"data=\"dl:{_esc(r['videoId'])}\">{i}</tg-button>"
            for i, r in enumerate(res, 1))
        rich = (
            "<h3>🎵 Results for “" + _esc(query) + "”</h3>"
            "<table bordered compact>"
            "<tr><th>#</th><th>Track</th><th>Artist</th><th>Length</th></tr>"
            + "".join(rows) +
            "</table>"
            f"<p>Tap to download: {buttons}</p>"
            "<footer>ytm-api • full song ≈ <tg-math>"
            r"t_{dl} \approx \dfrac{size}{v} + t_{mux}"
            "</tg-math> • mp4 + cover art</footer>")
        simple = ("<b>🎵 Results for “" + _esc(query) + "”</b>\n" + "\n".join(
            f"{i}. <b>{_esc(r.get('title'))}</b> — {_esc(r.get('artist') or '')} "
            f"({_esc(r.get('duration') or '–')})"
            for i, r in enumerate(res, 1)) +
            "\nReply with a number 1-5 to download.")
        # groups: results visible only to the requester (Bot API 10.2
        # ephemeral messages); fallback = normal message
        r = self._send(chat_id, rich, reply_to=None if is_group else mid,
                       ephemeral_user=user_id if is_group else None,
                       simple_html=simple)
        if not r.get("ok"):
            self._send(chat_id, simple, reply_to=mid)

    def _cmd_status(self, chat_id: int, mid: int):
        dls = self.engine.list_downloads()[:8]
        rows = []
        for d in dls:
            tr = d.get("track") or {}
            title = _esc((tr.get("title") or d.get("filename") or
                          d.get("videoId") or "")[:34])
            mb = (d.get("sizeBytes") or d.get("bytesDone") or 0) / 1e6
            rows.append(
                f"<tr><td>{title}</td>"
                f"<td align=\"center\"><code>{_esc(d.get('status'))}</code></td>"
                f"<td align=\"right\">{mb:.1f} MB</td>"
                f"<td align=\"center\">{_esc(d.get('mode') or '–')}</td></tr>")
        if not rows:
            self._send(chat_id, "<p>No downloads yet. Try <code>/get …</code>"
                                "</p>", reply_to=mid)
            return
        rich = ("<h3>📥 Recent downloads</h3>"
                "<table bordered striped compact>"
                "<tr><th>Track</th><th>Status</th><th>Size</th><th>Mode</th></tr>"
                + "".join(rows) + "</table>"
                "<footer><tg-math-block>"
                r"\overline{t}=\frac{1}{n}\sum_{i=1}^{n} t_i \quad\text{(avg "
                r"\approx 39s/song incl. mp4 + push)}"
                "</tg-math-block></footer>")
        simple = "<b>📥 Recent downloads</b>\n" + "\n".join(
            f"• {_esc((d.get('track') or {}).get('title') or '?')} — "
            f"{_esc(d.get('status'))}" for d in dls)
        self._send(chat_id, rich, reply_to=mid, simple_html=simple)

    def _cmd_help(self, chat_id: int, mid: int):
        rich = (
            "<h2>🎧 ytm-api bot</h2>"
            "<p>Search &amp; download full songs from YouTube Music as "
            "<b>mp4 with cover art</b> — straight into this chat.</p>"
            "<ul>"
            "<li><code>/get kesariya</code> — search, then tap a result</li>"
            "<li><code>/status</code> — recent downloads</li>"
            "<li><code>1</code>…<code>5</code> — pick from the last results</li>"
            "</ul>"
            "<blockquote expandable>Every download runs through the "
            "SABR in-browser pipeline: the player streams with a real PO "
            "token, we replay the ranged GET in-page, strip UMP framing "
            "and remux to mp4 — no realtime recording.</blockquote>"
            "<tg-math-block>"
            r"\mathrm{speed}=\frac{size}{t} \approx "
            r"\frac{5\,\text{MB}}{27\,\text{s}}\approx 190\,\text{kB/s}"
            "</tg-math-block>"
            "<footer>pure python + seleniumbase • no premade yt libs</footer>")
        simple = ("<b>🎧 ytm-api bot</b>\n"
                  "/get &lt;song&gt; — search &amp; download\n"
                  "/status — recent downloads\n"
                  "1-5 — pick from last results")
        self._send(chat_id, rich, reply_to=mid, simple_html=simple)

    # ---------- callbacks / download ----------
    def _on_callback(self, cb: Dict):
        data = cb.get("data") or ""
        tok = self._token_raw()
        self._api(tok, "answerCallbackQuery", json={
            "callback_query_id": cb.get("id")})
        m = re.match(r"^dl:([0-9A-Za-z_-]{11})$", data)
        if not m:
            return
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id:
            chat_id = ((cb.get("from") or {}).get("id"))
        frm = (cb.get("from") or {}).get("id")
        # find the cached result entry (title/artist)
        entry = None
        for res in (self._pending.get(str(chat_id)) or []):
            if res["videoId"] == m.group(1):
                entry = res
                break
        if entry is None:
            entry = {"videoId": m.group(1), "title": m.group(1),
                     "artist": "YouTube Music"}
        self._start_download(chat_id, entry, user_id=frm)

    def _start_download(self, chat_id: int, entry: Dict, user_id: int = None):
        vid = entry["videoId"]
        key = f"{chat_id}:{vid}"
        with self._lock:
            if self._inflight.get(key):
                return
            self._inflight[key] = True
        title = entry.get("title") or vid
        artist = entry.get("artist") or ""
        is_group = chat_id < 0
        rich = (f"<p>⬇️ <b>{_esc(title)}</b>"
                + (f" — {_esc(artist)}" if artist else "")
                + "<br/><code>resolving…</code></p>")
        r = self._send(chat_id, rich,
                       ephemeral_user=user_id if is_group else None)
        prog_msg = (r.get("result") or {}).get("message_id") if r.get("ok") \
            else None
        prog_eph = bool(r.get("result", {}).get("is_ephemeral")) \
            if r.get("ok") else False
        dl_id = self.engine.start_download(vid, notify_chat=chat_id)
        threading.Thread(target=self._watch, daemon=True,
                         args=(chat_id, prog_msg, prog_eph, dl_id, title,
                               vid, key)).start()

    def _watch(self, chat_id, prog_msg, prog_eph, dl_id, title, vid, key):
        try:
            last = None
            t0 = time.time()
            while time.time() - t0 < 240:
                st = self.engine.downloads.get(dl_id, {})
                cur = st.get("status")
                if cur != last:
                    last = cur
                    mb = (st.get("bytesDone") or 0) / 1e6
                    mode = st.get("mode") or ""
                    icon = {"resolving": "🔎", "sabr": "⚡", "downloading": "⬇️",
                            "capturing": "⏺", "processing": "🎚",
                            "sending": "📤", "done": "✅"}.get(cur, "•")
                    body = (f"<p>{icon} <b>{_esc(title)}</b><br/>"
                            f"<code>{_esc(cur or 'queued')}</code>"
                            + (f" • {mb:.1f} MB" if mb else "")
                            + (f" • {mode}" if mode else "")
                            + f" • {time.time()-t0:.0f}s</p>")
                    if prog_msg:
                        if prog_eph:
                            tok = self._token_raw()
                            rr = self._api(tok, "editEphemeralMessageText",
                                           json={"chat_id": chat_id,
                                                 "message_id": prog_msg,
                                                 "rich_message": {"html": body}})
                            if not rr.get("ok"):
                                self._api(tok, "editMessageText", json={
                                    "chat_id": chat_id,
                                    "message_id": prog_msg,
                                    "text": _strip_html(body)})
                        else:
                            self._edit(chat_id, prog_msg, body)
                if cur in ("done", "error"):
                    if cur == "error":
                        err = _esc((st.get("error") or "unknown")[:200])
                        if prog_msg:
                            self._edit(chat_id, prog_msg,
                                       f"<p>❌ <b>{_esc(title)}</b><br/>"
                                       f"<code>{err}</code></p>")
                    break
                time.sleep(1.0)
        except Exception:
            pass
        finally:
            with self._lock:
                self._inflight.pop(key, None)


_bot: Optional[YTMusicBot] = None
_once = threading.Lock()


def get_bot(base_dir: str, engine: Engine) -> YTMusicBot:
    global _bot
    with _once:
        if _bot is None:
            _bot = YTMusicBot(base_dir, engine)
    return _bot
