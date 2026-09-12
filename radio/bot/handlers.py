"""Command + callback handlers for the AshXMusic bot.

Commands:
  /start /help     banner + command guide (+ live buttons)
  /play <song>     search YT Music, play NEXT (plain text works too)
  /add <song>      append to the end of the queue
  /search <song>   5 results, tap one to queue it
  /skip            cut the current song
  /pause /resume   pause/resume playback (slate keeps the stream alive)
  /np              now playing + fresh control panel
  /queue [n]       show queue (with ❌ remove buttons)
  /autoplay on|off YT-Music-style autoplay after the queue drains
  /loop off|one|all
  /shuffle on|off
  /volume 0-150    render-level volume (applies to the next renders)
  /status          stream health: on air, reconnects, stall info
  /stats           songs played, uptime, cache size
  /history         last played
  /configure ...   set RTMP server + key, restart the workflow
  /config          show current stream config
  /stopstream      stop the 24/7 chain
  /id              chat id
  /ping            latency check
"""
import os
import time

from radio import stateio
from radio import station as _ST
from radio.bot import configure as cfg

REPO_URL = "https://github.com/" + os.environ.get("GITHUB_REPOSITORY",
                                                  "idofone3/AshXMusic")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;")


HELP_TEXT = (
    "🎵 <b>AshXMusic</b> — 24/7 YouTube Music radio\n\n"
    "<b>Listening</b>\n"
    "• Just send any <b>song name</b> — it plays next\n"
    "• /play &lt;song&gt; — same as above\n"
    "• /add &lt;song&gt; — add to the end of the queue\n"
    "• /search &lt;song&gt; — pick from 5 results\n\n"
    "<b>Playback</b>\n"
    "• /pause • /resume • /skip • /np\n"
    "• /loop off|one|all • /shuffle on|off\n"
    "• /volume 0-150 (next renders)\n\n"
    "<b>Queue &amp; radio</b>\n"
    "• /queue — what's lined up\n"
    "• /autoplay on|off — auto-pick hits when queue drains\n"
    "• /history — recently played\n\n"
    "<b>Stream control</b>\n"
    "• /status — on-air health\n"
    "• /stats — totals &amp; uptime\n"
    "• /configure &lt;server&gt; &lt;key&gt; — new RTMP creds + auto-restart\n"
    "• /config — show current creds\n"
    "• /stopstream — halt the 24/7 chain\n\n"
    "💡 Buttons under the Now-Playing message are a full remote: "
    "⏸ play-pause, ⏭ skip, 🔁 loop, 🔀 shuffle, 🤖 autoplay.")


class Handlers:
    def __init__(self, bot):
        self.bot = bot
        self.pending = {}   # chat_id -> list of search hits (for pick:N)

    # ---------- text commands ----------

    def handle_message(self, msg: dict):
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text or not chat:
            return
        if self.bot.control and chat != self.bot.control:
            return  # only the control chat can steer
        if text.lower().startswith("/"):
            parts = text.split(None, 1)
            cmd = parts[0].split("@")[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            fn = getattr(self, "cmd_" + cmd[1:].replace("/", "_"), None)
            if fn:
                try:
                    fn(chat, arg, msg)
                except Exception as e:
                    self.bot.api.send_message(
                        chat, f"😕 error: <code>{_esc(str(e)[:180])}</code>")
            else:
                self.bot.api.send_message(
                    chat, f"Unknown command {cmd} — try /help")
            return
        # plain text = song request (YT-Music style)
        self.cmd_play(chat, text, msg)

    # ---------- listening ----------

    def cmd_start(self, chat, arg, msg):
        logo = os.path.join(self.bot.repo_dir, "assets", "logo.png")
        if os.path.exists(logo):
            self.bot.api.send_photo(
                chat, logo,
                caption="🎵 <b>AshXMusic is live</b>\nSend any song name — "
                        "it plays next on the radio!",
                kb=self.bot.kb_np())
        else:
            self.bot.api.send_message(chat, HELP_TEXT, kb=self.bot.kb_np())
        self.cmd_np(chat, "", msg, quiet=True)

    def cmd_help(self, chat, arg, msg):
        from radio.bot import keyboards
        self.bot.api.send_message(chat, HELP_TEXT,
                                  kb=keyboards.help_kb(REPO_URL))

    def cmd_play(self, chat, arg, msg, front=True):
        if not arg:
            self.bot.api.send_message(chat, "Usage: /play &lt;song name&gt;")
            return
        self.bot.api.chat_action(chat, "typing")
        ok = self.bot.station.request_song(arg, front=front)
        if ok:
            pos = "top of the queue — playing next" if front else "queue"
            self.bot.api.send_message(
                chat, f"🎵 <b>{_esc(arg)}</b> queued ({pos})\n"
                      f"searching → downloading → on air…")
        else:
            self.bot.api.send_message(
                chat, "⏳ Another search is still running — try again "
                      "in a few seconds.")

    def cmd_add(self, chat, arg, msg):
        if not arg:
            self.bot.api.send_message(chat, "Usage: /add &lt;song name&gt;")
            return
        self.cmd_play(chat, arg, msg, front=False)

    def cmd_search(self, chat, arg, msg):
        if not arg:
            self.bot.api.send_message(chat, "Usage: /search &lt;song&gt;")
            return
        st = self.bot.station

        def work():
            try:
                hits = st.eng.search(arg, flt="songs", limit=10)
            except Exception as e:
                self.bot.api.send_message(
                    chat, f"😕 search failed: <code>{_esc(str(e)[:150])}"
                          f"</code>")
                return
            hits = st._filter_hits(hits)[:5] or hits[:5]
            if not hits:
                self.bot.api.send_message(chat, f"No results for "
                                                f"“{_esc(arg)}”")
                return
            self.pending[chat] = hits
            lines = [f"🔎 <b>Results for “{_esc(arg)}”</b>\n"]
            for i, h in enumerate(hits, 1):
                lines.append(f"{i}. <b>{_esc(h.get('title') or '?')}</b>\n"
                             f"    {_esc(h.get('artist') or '')} • "
                             f"{_esc(h.get('duration') or '?')}")
            lines.append("\nTap a number to queue it:")
            from radio.bot import keyboards
            self.bot.api.send_message(chat, "\n".join(lines),
                                      kb=keyboards.search_kb(len(hits)))
        import threading
        threading.Thread(target=work, daemon=True).start()

    # ---------- playback ----------

    def cmd_skip(self, chat, arg, msg):
        self.bot.station.request_skip()
        self.bot.api.send_message(chat, "⏭️ Skipping…")

    def cmd_pause(self, chat, arg, msg):
        st = self.bot.station
        if st.request_pause():
            self.bot.api.send_message(chat, "⏸ Paused — /resume to continue",
                                      kb=self.bot.kb_np(paused=True))
        else:
            self.bot.api.send_message(chat, "Nothing is playing right now.")

    def cmd_resume(self, chat, arg, msg):
        st = self.bot.station
        if st.request_resume():
            self.bot.api.send_message(chat, "▶️ Resumed — music is back!")
            self.bot.panel.refresh_now()
        else:
            self.bot.api.send_message(chat, "Nothing was paused.")

    def cmd_np(self, chat, arg, msg, quiet=False):
        st = self.bot.station
        e = st.current_entry()
        if not e:
            if not quiet:
                q = st.queue_snapshot(1)
                hint = (f"💤 Idle — up next: <b>{_esc(q[0].get('title'))}"
                        f"</b>" if q else
                        "💤 Idle — send a song name to start!")
                self.bot.api.send_message(chat, hint, kb=self.bot.kb_np())
            return
        # fresh live panel
        cov = os.path.join(_ST.COVERS, f"{e.get('videoId')}.jpg")
        self.bot.panel.start(e, cover_path=cov if os.path.exists(cov) else "")

    def cmd_queue(self, chat, arg, msg):
        q = self.bot.station.queue_snapshot(8)
        if not q:
            auto = self.bot.station.state.get("autoplay", True)
            self.bot.api.send_message(
                chat, "Queue is empty — " +
                      ("radio autoplay picks the next hit 🤖" if auto else
                       "autoplay is OFF, use /play 🎵"))
            return
        lines = ["<b>📋 Up next</b>"]
        for i, e in enumerate(q, 1):
            tag = "⭐" if e.get("by") else "•"
            lines.append(f"{i}. {tag} <b>{_esc(e.get('title', '?'))}</b> — "
                         f"{_esc(e.get('artist', '?'))}")
        from radio.bot import keyboards
        self.bot.api.send_message(chat, "\n".join(lines),
                                  kb=keyboards.queue_kb(len(q)))

    # ---------- modes ----------

    def cmd_autoplay(self, chat, arg, msg):
        val = arg.lower().strip()
        st = self.bot.station
        if val in ("on", "1", "true", "yes"):
            st.state["autoplay"] = True
        elif val in ("off", "0", "false", "no"):
            st.state["autoplay"] = False
        else:
            cur = st.state.get("autoplay", True)
            st.state["autoplay"] = not cur
        st.state_changed = True
        on = st.state.get("autoplay")
        self.bot.api.send_message(
            chat, f"🤖 Autoplay <b>{'ON' if on else 'OFF'}</b> — " +
                  ("radio keeps picking hits after the queue drains."
                   if on else "queue drains to idle; /play to fill it."))

    def cmd_loop(self, chat, arg, msg):
        val = arg.lower().strip()
        if val in ("one", "1", "single", "this"):
            v = "one"
        elif val in ("all", "queue"):
            v = "all"
        elif val in ("off", "0", "none"):
            v = "off"
        else:  # cycle off -> one -> all
            cur = self.bot.station.state.get("loop", "off")
            v = {"off": "one", "one": "all", "all": "off"}.get(cur, "off")
        self.bot.station.state["loop"] = v
        self.bot.station.state_changed = True
        lbl = {"off": "off — queue plays normally", "one": "ONE — current "
               "song repeats", "all": "ALL — queue circles"}[v]
        self.bot.api.send_message(chat, f"🔁 Loop: <b>{v.upper()}</b> ({lbl})")

    def cmd_shuffle(self, chat, arg, msg):
        val = arg.lower().strip()
        st = self.bot.station
        if val in ("on", "1", "true", "yes"):
            st.state["shuffle"] = True
        elif val in ("off", "0", "false", "no"):
            st.state["shuffle"] = False
        else:
            st.state["shuffle"] = not st.state.get("shuffle", False)
        st.state_changed = True
        on = st.state.get("shuffle")
        self.bot.api.send_message(
            chat, f"🔀 Shuffle <b>{'ON' if on else 'OFF'}</b>")

    def cmd_volume(self, chat, arg, msg):
        st = self.bot.station
        try:
            v = max(0, min(150, int(float(arg))))
        except ValueError:
            v = -1
        if v < 0:
            self.bot.api.send_message(
                chat, f"🔊 Volume: <b>{int(st.state.get('volume', 100))}%"
                      f"</b> — usage: /volume 0-150 (applies to next "
                      f"rendered songs)")
            return
        st.state["volume"] = float(v)
        st.state_changed = True
        st.invalidate_renders()
        self.bot.api.send_message(
            chat, f"🔊 Volume set to <b>{v}%</b> — queued songs re-render "
                  f"with the new level.")

    # ---------- info ----------

    def cmd_status(self, chat, arg, msg):
        st = self.bot.station
        on_air = bool(st.current and st.current.get("videoId"))
        paused = st.pause_ev.is_set() if hasattr(st, "pause_ev") else False
        e = st.current_entry()
        lines = ["📡 <b>AshXMusic status</b>",
                 f"on air: <b>{'yes' if on_air else 'no'}</b>"
                 f"{' (paused)' if paused else ''}"]
        if e:
            lines.append(f"playing: <b>{_esc(e.get('title'))}</b> • "
                         f"{int(st.elapsed())}s")
        lines += [f"queue: {len(st.queue_snapshot(50))} • "
                  f"autoplay: {'on' if st.state.get('autoplay') else 'off'} • "
                  f"loop: {st.state.get('loop', 'off')} • "
                  f"shuffle: {'on' if st.state.get('shuffle') else 'off'}",
                  f"endpoint: <code>{_esc(cfg.mask_url(st.rtmp))}</code>"]
        self.bot.api.send_message(chat, "\n".join(lines))

    def cmd_stats(self, chat, arg, msg):
        st = self.bot.station
        up = time.time() - getattr(st, "started_at", time.time())
        h, m = int(up // 3600), int(up % 3600 // 60)
        cache = 0
        try:
            cache = sum(os.path.getsize(os.path.join(_ST.CACHE, f))
                        for f in os.listdir(_ST.CACHE)) / 1e6
        except Exception:
            pass
        played = int(st.state.get("songsPlayed", 0))
        self.bot.api.send_message(
            chat, f"📊 <b>AshXMusic stats</b>\n"
                  f"uptime: <b>{h}h {m}m</b>\n"
                  f"songs played: <b>{played}</b>\n"
                  f"history: {len(st.state.get('history', []))}\n"
                  f"cache: {cache:.0f} MB")

    def cmd_history(self, chat, arg, msg):
        h = (self.bot.station.state.get("history") or [])[-10:][::-1]
        if not h:
            self.bot.api.send_message(chat, "No history yet.")
            return
        lines = ["<b>🕘 Recently played</b>"]
        for i, e in enumerate(h, 1):
            ts = time.strftime("%H:%M", time.localtime(e.get("at", 0)))
            lines.append(f"{i}. <b>{_esc(e.get('title') or '?')}</b> • {ts}")
        self.bot.api.send_message(chat, "\n".join(lines))

    def cmd_ping(self, chat, arg, msg):
        t0 = time.time()
        r = self.bot.api.call("getMe", timeout=10)
        ms = (time.time() - t0) * 1000
        self.bot.api.send_message(
            chat, f"🏓 pong — bot API {ms:.0f}ms"
                  f"{' • stream live' if self.bot.station.current else ''}")

    def cmd_id(self, chat, arg, msg):
        self.bot.api.send_message(chat, f"🆔 chat id: <code>{chat}</code>")

    # ---------- stream control ----------

    def cmd_configure(self, chat, arg, msg):
        if not arg:
            cfg.run_status_config(self.bot, chat)
            return
        cfg.run_configure(self.bot, chat, arg)

    def cmd_config(self, chat, arg, msg):
        cfg.run_status_config(self.bot, chat)

    def cmd_stopstream(self, chat, arg, msg):
        stateio.set_stop()
        try:
            stateio.push_state_branch(self.bot.repo_dir)
        except Exception:
            pass
        self.bot.api.send_message(
            chat, "🛑 <b>Stream stopping.</b> The station exits and the "
                  "24/7 chain halts (STOP marker saved).\n"
                  "Start again: Actions → AshXMusic → Run workflow, or "
                  "/configure <server> <key>.")
        self.bot.station.request_skip()   # unblock the pump

    # ---------- button callbacks ----------

    def handle_callback(self, cq: dict):
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        data = cq.get("data", "")
        cid = cq.get("id")
        if self.bot.control and chat != self.bot.control:
            self.bot.api.answer_callback(cid, "Not your remote 😉")
            return
        if data == "pick:cancel":
            self.pending.pop(chat, None)
            self.bot.api.answer_callback(cid, "Cancelled")
            try:
                self.bot.api.delete_message(chat, cq["message"]["message_id"])
            except Exception:
                pass
            return
        if data.startswith("pick:"):
            hits = self.pending.get(chat) or []
            try:
                idx = int(data.split(":")[1])
            except ValueError:
                idx = -1
            if 0 <= idx < len(hits):
                h = hits[idx]
                self.bot.station.request_video(h)
                self.bot.api.answer_callback(
                    cid, f"Queued: {h.get('title', '')[:60]}")
                try:
                    self.bot.api.delete_message(
                        chat, cq["message"]["message_id"])
                except Exception:
                    pass
                self.pending.pop(chat, None)
            else:
                self.bot.api.answer_callback(cid, "Expired — /search again")
            return
        if data.startswith("rm:"):
            try:
                idx = int(data.split(":")[1])
            except ValueError:
                idx = -1
            q = self.bot.station.queue_snapshot(50)
            if 0 <= idx < len(q):
                self.bot.station.remove_at(idx)
                self.bot.api.answer_callback(
                    cid, f"Removed: {q[idx].get('title', '')[:50]}")
                self.cmd_queue(chat, "", {})
            else:
                self.bot.api.answer_callback(cid, "Already gone")
            return
        st = self.bot.station
        if data == "act:pause":
            if st.request_pause():
                self.bot.api.answer_callback(cid, "⏸ Paused")
            else:
                self.bot.api.answer_callback(cid, "Nothing playing")
            self.bot.panel.refresh_now()
        elif data == "act:resume":
            if st.request_resume():
                self.bot.api.answer_callback(cid, "▶ Resumed")
            else:
                self.bot.api.answer_callback(cid, "Not paused")
            self.bot.panel.refresh_now()
        elif data == "act:skip":
            st.request_skip()
            self.bot.api.answer_callback(cid, "⏭ Skipped")
        elif data == "act:loop":
            self.cmd_loop(chat, "", {})
            self.bot.api.answer_callback(
                cid, f"Loop: {st.state.get('loop', 'off')}")
        elif data == "act:shuffle":
            self.cmd_shuffle(chat, "", {})
            self.bot.api.answer_callback(
                cid, f"Shuffle: {'on' if st.state.get('shuffle') else 'off'}")
        elif data == "act:autoplay":
            self.cmd_autoplay(chat, "", {})
            self.bot.api.answer_callback(
                cid, f"Autoplay: {'on' if st.state.get('autoplay') else 'off'}")
        elif data == "act:queue":
            self.cmd_queue(chat, "", {})
            self.bot.api.answer_callback(cid)
        elif data == "act:shufflequeue":
            st.shuffle_queue()
            self.bot.api.answer_callback(cid, "🔀 Queue shuffled")
            self.cmd_queue(chat, "", {})
        elif data == "act:clearqueue":
            st.clear_queue()
            self.bot.api.answer_callback(cid, "Queue cleared")
            self.cmd_queue(chat, "", {})
        else:
            self.bot.api.answer_callback(cid)
