"""24/7 radio station — YouTube Music -> Telegram RTMP.

Pipeline (all threads supervised, gap-free):
  tgbot ──requests──┐
  pool ──autoplay───┤
                    ▼
           [downloader]  cache/*.mp4 (ytm engine, SABR pyfetch)
                    ▼
           [renderer]    rendered/*.ts  (blur bg + cover + tracker UI)
                    ▼
           [pump]        remux ts with running timestamp offset -> FIFO
                    ▼
           [streamer]    ffmpeg -re FIFO -c copy -> rtmps://t.me

A 20s slate fills any gap so the live stream never goes black.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("YTM_NO_TG", "1")  # radio: no per-song file pushes

CACHE = os.path.join(HERE, "cache")
RENDERED = os.path.join(HERE, "rendered")
WORK = os.path.join(HERE, "work")
COVERS = os.path.join(HERE, "work", "covers")
for d in (CACHE, RENDERED, WORK, COVERS):
    os.makedirs(d, exist_ok=True)

FIFO = "/tmp/radio_stream.ts"
SLATE_TS = os.path.join(RENDERED, "__slate.ts")
SLATE_SEC = 20.0


def log(msg: str):
    print(f"[station {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ffprobe_dur(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path], capture_output=True, text=True, timeout=30)
        return float((json.loads(r.stdout or "{}").get("format") or {})
                     .get("duration") or 0)
    except Exception:
        return 0.0


def cache_find(video_id: str):
    try:
        for name in os.listdir(CACHE):
            if name.endswith(f"[{video_id}].mp4"):
                return os.path.join(CACHE, name)
    except OSError:
        pass
    return None


class Station:
    def __init__(self, args):
        self.args = args
        self.alive = True
        self.deadline = time.time() + args.hours * 3600
        self.qlock = threading.RLock()
        self.skip_ev = threading.Event()     # pump: cut current file
        self.jump_ev = threading.Event()     # renderer: abort current render
        self.searching = False               # one search at a time
        self.current = None                  # entry being pumped
        self.current_t0 = 0.0
        self.np_done = set()                 # videoIds already announced
        self._retries = {}                   # videoId -> failed push count
        self.tg = None                       # wired in run()
        import stateio as S
        self.S = S
        if args.rtmp.endswith("/"):
            self.rtmp = args.rtmp + args.key
        else:
            self.rtmp = args.rtmp + "/" + args.key
        st = S.load()
        # restored entries carry absolute paths from the PREVIOUS runner —
        # drop them; the downloader re-finds files in the restored cache
        for e in st.get("queue", []):
            for k in ("file", "ts", "rfail"):
                e.pop(k, None)
        self.state = st
        log(f"state loaded: {len(st.get('queue', []))} queued, "
            f"{len(st.get('history', []))} in history")

        # ---- engine (search + SABR download) ----
        from ytm.engine import Engine
        self.eng = Engine(cookie_path=os.path.join(ROOT, "data", "cookies.txt"),
                          downloads_dir=CACHE)
        self.eng.allow_capture = False
        log("engine ready (prewarmed)")

        from radio import pool  # noqa: F401  (import check)
        import radio.pool as P
        self.pool = P

    # ---------------- queue helpers ----------------

    def log(self, m):
        log(m)

    def _filter_hits(self, hits, tkw="", akw="", strict=False):
        bad = self.pool.BADWORDS
        clean = []
        for t in hits:
            title = (t.get("title") or "").lower()
            artist = (t.get("artist") or "").lower()
            if any(w in title or w in artist for w in bad):
                continue
            clean.append(t)
        if tkw or akw:
            matched = [t for t in clean
                       if (not tkw or tkw in (t.get("title") or "").lower())
                       and (not akw or akw in (t.get("artist") or "").lower())]
            if matched:
                return matched
            # loose fallback only for user requests, never for pool picks
            return [] if strict else clean
        return clean

    def _search_video(self, query: str, tkw="", akw="", strict=False):
        try:
            hits = self.eng.search(query, flt="songs", limit=8)
        except Exception as e:
            self.log(f"search '{query}' failed: {e}")
            return None
        picks = self._filter_hits(hits, tkw, akw, strict=strict)
        return picks[0] if picks else None

    def request_song(self, query: str, front: bool = True) -> bool:
        if self.searching:
            return False
        self.searching = True

        def work():
            try:
                hit = self._search_video(query)
                if not hit:
                    if self.tg and self.tg.control:
                        self.tg.reply(self.tg.control,
                                      f"😕 Nothing found for <b>{query}</b>")
                    return
                entry = {"videoId": hit["videoId"],
                         "title": hit.get("title") or hit["videoId"],
                         "artist": hit.get("artist") or "Unknown",
                         "dur_str": hit.get("duration") or "",
                         "by": query, "added": time.time()}
                with self.qlock:
                    if front:
                        self.state["queue"].insert(0, entry)
                    else:
                        self.state["queue"].append(entry)
                self.log(f"queued {'(front) ' if front else ''}"
                         f"{entry['title']} [{entry['videoId']}]")
                if front:
                    self.jump_ev.set()   # renderer: serve this first
                    self.skip_ev.set()   # pump: cut current song
            finally:
                self.searching = False
        threading.Thread(target=work, daemon=True).start()
        return True

    def request_skip(self):
        self.skip_ev.set()

    def current_entry(self):
        with self.qlock:
            return dict(self.current) if self.current else None

    def elapsed(self):
        return max(0.0, time.time() - self.current_t0) if self.current else 0.0

    def queue_snapshot(self, n=8):
        with self.qlock:
            return [dict(e) for e in self.state["queue"][:n]]

    def _drop(self, idx: int):
        with self.qlock:
            if 0 <= idx < len(self.state["queue"]):
                self.state["queue"].pop(idx)

    # ---------------- downloader ----------------

    def downloader_loop(self):
        picks = 0
        while self.alive:
            try:
                with self.qlock:
                    queue = self.state["queue"]
                    # 1) keep the radio queue stocked (autoplay mix)
                    if len(queue) < 6:
                        recent = {e.get("videoId", "") for e in queue}
                        recent |= {h.get("videoId", "") for h in
                                   self.state["history"][-30:]}
                        rec_names = {e.get("title", "").lower()
                                     for e in queue}
                        rec_names |= {h.get("title", "").lower() for h in
                                      self.state["history"][-30:]}
                        p = self.pool.pick(__import__("random"), rec_names)
                        queue.append({"videoId": None, "q": p["q"],
                                      "tkw": p["tkw"], "akw": p["akw"],
                                      "by": None, "added": time.time()})
                    # 2) resolve pool picks -> videoId
                    for i, e in enumerate(queue[:4]):
                        if e.get("videoId") is None:
                            hit = self._search_video(e["q"], e.get("tkw", ""),
                                                     e.get("akw", ""),
                                                     strict=True)
                            if hit:
                                e["videoId"] = hit["videoId"]
                                e["title"] = hit.get("title") or e["q"]
                                e["artist"] = hit.get("artist") or "Unknown"
                                e["dur_str"] = hit.get("duration") or ""
                                self.log(f"pool resolved: {e['title']}")
                            else:
                                self.log(f"pool skip (no clean match): {e['q']}")
                                self._drop(i)
                            break  # one search per pass
                    # 3) ensure files for the first two entries
                with self.qlock:
                    need = [e for e in self.state["queue"][:2]
                            if e.get("videoId") and not e.get("file")]
                for e in need:
                    vid = e["videoId"]
                    path = cache_find(vid)
                    if not path:
                        dl = self.eng.start_download(vid)
                        t0 = time.time()
                        ok = False
                        while time.time() - t0 < 300 and self.alive:
                            st = self.eng.downloads.get(dl, {})
                            if st.get("status") == "done":
                                ok = True
                                break
                            if st.get("status") == "error":
                                break
                            time.sleep(1.5)
                        st = self.eng.downloads.get(dl, {})
                        if ok and st.get("file"):
                            path = st["file"]
                        else:
                            tiers = st.get("tierErrors") or {}
                            self.log(f"download failed {vid}: "
                                     f"{st.get('error') or 'timeout'} "
                                     f"tiers={list(tiers.keys())} "
                                     f"detail={ (tiers.get('sabr') or '')[:120] }")
                            try:
                                from ytm import fastdl as _fdl
                                _fdl._SHARED.invalidate()
                            except Exception:
                                pass
                            # give up on this entry (radio moves on)
                            with self.qlock:
                                try:
                                    self.state["queue"].remove(e)
                                except ValueError:
                                    pass
                            continue
                    if path:
                        e["file"] = path
                        e["dur"] = e.get("dur") or ffprobe_dur(path)
                        self.log(f"ready: {e.get('title')} "
                                 f"({e.get('dur', 0):.0f}s)")
            except Exception as ex:
                self.log(f"downloader: {ex}")
                time.sleep(5)
            time.sleep(2)

    # ---------------- renderer ----------------

    def _cover_for(self, vid: str) -> str:
        cov = os.path.join(COVERS, f"{vid}.jpg")
        if os.path.exists(cov):
            return cov
        # raw 16:9 thumb (no grey pillarbox bars) -> visuals center-crops
        import requests
        for url in (f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
                    f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"):
            try:
                r = requests.get(url, timeout=(10, 20), headers={
                    "User-Agent": "Mozilla/5.0", "Referer":
                    "https://music.youtube.com/"})
                ct = r.headers.get("content-type", "")
                if r.status_code == 200 and "image" in ct \
                        and len(r.content) > 2000:
                    with open(cov, "wb") as f:
                        f.write(r.content)
                    return cov
            except Exception:
                continue
        # fallback: neutral default cover so rendering never blocks
        default = os.path.join(COVERS, "default.jpg")
        if not os.path.exists(default):
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i", "color=c=0x2A2F3A:s=640x640",
                     "-frames:v", "1", "-update", "1", default],
                    check=True, timeout=60)
            except Exception:
                return ""
        return default if os.path.exists(default) else ""

    def _render_slate(self):
        if not hasattr(self, "_slate_lock"):
            self._slate_lock = threading.Lock()
        with self._slate_lock:
            if os.path.exists(SLATE_TS) and os.path.getsize(SLATE_TS) > 10000:
                return
            self._render_slate_inner()

    def _render_slate_inner(self):
        from radio import visuals as V
        graph = V.build_slate_graph(WORK)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"color=c=0x10131C:s={V.W}x{V.H}:r=30",
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-filter_complex", graph, "-map", "[vout]", "-map", "1:a"]
        cmd += V.ffmpeg_common_args(SLATE_TS + ".tmp", SLATE_SEC)
        try:
            subprocess.run(cmd, check=True, timeout=180,
                           stdin=subprocess.DEVNULL)
            os.replace(SLATE_TS + ".tmp", SLATE_TS)
            self.log("slate rendered")
        except Exception as e:
            self.log(f"slate render failed: {e}")
            time.sleep(3)

    def renderer_loop(self):
        from radio import visuals as V
        while self.alive:
            try:
                self._render_slate()
                target = None
                with self.qlock:
                    for e in self.state["queue"]:
                        if e.get("videoId") and e.get("file") \
                                and not e.get("ts") and not e.get("rfail"):
                            target = e
                            break
                if not target:
                    self.jump_ev.wait(1.0)
                    self.jump_ev.clear()
                    continue
                vid = target["videoId"]
                ts_path = os.path.join(RENDERED, f"{vid}.ts")
                if os.path.exists(ts_path):
                    target["ts"] = ts_path
                    continue
                dur = target.get("dur") or ffprobe_dur(target["file"]) or 180.0
                cover = self._cover_for(vid)
                self.log(f"rendering: {target.get('title')}")
                graph = V.build_song_graph(target.get("title", ""),
                                           target.get("artist", ""),
                                           dur, WORK)
                cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                       "-i", target["file"], "-loop", "1", "-i", cover,
                       "-filter_complex", graph,
                       "-map", "[vout]", "-map", "0:a"]
                cmd += V.ffmpeg_common_args(ts_path + ".tmp", dur)
                p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL)
                t0 = time.time()
                while p.poll() is None and time.time() - t0 < 600:
                    if self.jump_ev.is_set():
                        p.terminate()
                        break
                    time.sleep(0.5)
                if p.poll() is None:
                    p.terminate()
                if os.path.exists(ts_path + ".tmp") and \
                        os.path.getsize(ts_path + ".tmp") > 50000 \
                        and not self.jump_ev.is_set():
                    os.replace(ts_path + ".tmp", ts_path)
                    target["ts"] = ts_path
                    target["dur"] = dur
                    self.log(f"rendered: {target.get('title')}")
                else:
                    try:
                        os.remove(ts_path + ".tmp")
                    except OSError:
                        pass
                    if self.jump_ev.is_set():
                        self.jump_ev.clear()
                        continue  # aborted to serve a request first
                    target["rfail"] = True
                    self.log(f"render failed: {target.get('title')}")
            except Exception as ex:
                self.log(f"renderer: {ex}")
                time.sleep(3)

    # ---------------- pump + streamer ----------------

    def _notify_np(self, entry):
        if entry["videoId"] in self.np_done:
            return
        self.np_done.add(entry["videoId"])
        if len(self.np_done) > 500:
            self.np_done.clear()
        cov = os.path.join(COVERS, f"{entry['videoId']}.jpg")
        try:
            self.tg.notify_nowplaying(entry, cov)
        except Exception:
            pass

    def _pump_once(self, src: str, offset: float, seconds: float) -> float:
        """Remux src into the fifo with a timestamp offset; returns the
        seconds actually played (cuts early on skip / streamer death)."""
        from radio import visuals as V
        prog = os.path.join(WORK, "remux_progress.txt")
        try:
            os.remove(prog)
        except OSError:
            pass
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
               "-i", src, "-c", "copy", "-muxdelay", "0", "-muxpreload", "0",
               "-mpegts_flags", "+resend_headers",
               "-output_ts_offset", f"{offset:.3f}",
               "-progress", prog, "-nostats",
               "-f", "mpegts", FIFO]
        remux_log = os.path.join(WORK, "remux.log")
        remux_err = open(remux_log, "ab")
        t0 = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=remux_err, stdin=subprocess.DEVNULL)
        remux_err.close()
        last_out_time = -1.0
        last_advance = time.time()
        while p.poll() is None and self.alive:
            if self.skip_ev.is_set():
                p.terminate()
                break
            # stall watchdog: if the muxer hasn't advanced in 60s the
            # downstream RTMP connection is not consuming — cut it
            ot = self._read_out_time(prog)
            if ot > last_out_time:
                last_out_time = ot
                last_advance = time.time()
            elif time.time() - last_advance > 60:
                self.log(f"remux stalled (muxed {ot:.0f}s) — watchdog cut")
                p.terminate()
                break
            time.sleep(0.5)
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:
                p.kill()
        played = time.time() - t0
        ot = self._read_out_time(prog)
        if p.returncode == 0:
            # natural end — wallclock is a fine approximation
            played = max(played, ot if ot > 0 else 0)
        else:
            # killed (skip / watchdog) — trust the muxer's counter only;
            # an unreadable counter means nothing reached the stream
            played = ot if ot > 0 else 0.0
        return max(0.0, min(played, seconds + 2.0))

    def _read_out_time(self, prog: str) -> float:
        """Last muxed output time in seconds (ffmpeg 6/7 progress keys)."""
        try:
            us = ms = None
            with open(prog) as f:
                for line in f:
                    if line.startswith("out_time_us="):
                        try:
                            us = float(line.split("=")[1]) / 1e6
                        except ValueError:
                            pass
                    elif line.startswith("out_time_ms="):
                        try:
                            ms = float(line.split("=")[1]) / 1e6
                        except ValueError:
                            pass
            if us is not None:
                return us
            if ms is not None:
                return ms
        except Exception:
            pass
        return -1.0

    def pipeline_loop(self):
        from radio import visuals as V
        # fresh fifo each run
        try:
            os.remove(FIFO)
        except OSError:
            pass
        try:
            os.mkfifo(FIFO)
        except FileExistsError:
            pass
        attempt = 0
        while self.alive and time.time() < self.deadline:
            attempt += 1
            self.log(f"streamer starting (attempt {attempt}) -> {self.rtmp}")
            streamer_log = os.path.join(WORK, "streamer.log")
            s_err = open(streamer_log, "ab")
            streamer = subprocess.Popen(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                 "-re", "-rw_timeout", "20000000",
                 "-fflags", "+genpts", "-i", FIFO,
                 "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
                 "-f", "flv", "-flvflags", "no_duration_filesize",
                 self.rtmp],
                stdout=subprocess.DEVNULL, stderr=s_err,
                stdin=subprocess.DEVNULL)
            s_err.close()
            try:
                self._pump_forever(streamer)
            except Exception as ex:
                self.log(f"pump error: {ex}")
            try:
                streamer.terminate()
                streamer.wait(5)
            except Exception:
                pass
            self._tail_log(os.path.join(WORK, "streamer.log"),
                           "streamer.log tail")
            if not self.alive or time.time() >= self.deadline:
                break
            self.log("streamer died — restarting in 5s")
            time.sleep(5)

    def _tail_log(self, path: str, label: str, lines: int = 6):
        try:
            with open(path, "rb") as f:
                tail = f.read()[-3000:].decode(errors="replace")
            for ln in tail.strip().splitlines()[-lines:]:
                if ln.strip():
                    self.log(f"{label}: {ln[:220]}")
        except Exception:
            pass

    def _pump_forever(self, streamer):
        off = 0.0
        stall_streak = 0
        warned = False
        self._render_slate()
        while self.alive and streamer.poll() is None \
                and time.time() < self.deadline:
            self.skip_ev.clear()
            if self.skip_ev.is_set():      # request landed between checks
                continue
            entry = None
            with self.qlock:
                for e in self.state["queue"]:
                    if e.get("ts"):
                        entry = e
                        break
            if entry is None:
                # gap filler
                if not os.path.exists(SLATE_TS):
                    self._render_slate()
                    if not os.path.exists(SLATE_TS):
                        time.sleep(2)
                        continue
                self.current = {"title": "Choosing the next song…",
                                "artist": "Hindi Hits Radio", "videoId": "—"}
                self.current_t0 = time.time()
                played = self._pump_once(SLATE_TS, off, SLATE_SEC)
                off += played
                if played < 5.0:
                    stall_streak += 1
                else:
                    stall_streak = 0
                    warned = False
                if stall_streak >= 2:
                    if not warned:
                        warned = True
                        self.log("⚠ RTMP endpoint is NOT consuming data — "
                                 "the live session for this key is probably "
                                 "not active. Will retry every ~30s and go "
                                 "live automatically once it accepts data.")
                    try:
                        streamer.terminate()   # force a clean reconnect
                    except Exception:
                        pass
                    time.sleep(30)
                continue
            self.current = entry
            self.current_t0 = time.time()
            self._notify_np(entry)
            dur = entry.get("dur") or ffprobe_dur(entry["ts"]) or 200.0
            self.log(f"ON AIR: {entry.get('title')} ({dur:.0f}s)")
            played = self._pump_once(entry["ts"], off, dur)
            off += played
            if played < 5.0:
                # nothing actually reached the stream — keep the song,
                # reconnect, and try again (endpoint dead / bad push)
                stall_streak += 1
                vid = entry.get("videoId")
                n = self._retries.get(vid, 0) + 1
                self._retries[vid] = n
                if n >= 3:
                    self.log(f"giving up on {entry.get('title')} after "
                             f"{n} failed pushes")
                    with self.qlock:
                        try:
                            self.state["queue"].remove(entry)
                        except ValueError:
                            pass
                    try:
                        os.remove(entry["ts"])
                    except OSError:
                        pass
                    self._retries.pop(vid, None)
                    stall_streak = 0
                if stall_streak >= 2:
                    if not warned:
                        warned = True
                        self.log("⚠ RTMP endpoint is NOT consuming data — "
                                 "the live session for this key is probably "
                                 "not active. Music resumes automatically "
                                 "once it accepts data.")
                    try:
                        streamer.terminate()
                    except Exception:
                        pass
                    time.sleep(30)
                self.current = None
                continue
            # healthy play
            stall_streak = 0
            warned = False
            self._retries.pop(entry.get("videoId"), None)
            self.current = None
            # consume the entry (queue + files) unless it must be kept
            with self.qlock:
                try:
                    self.state["queue"].remove(entry)
                except ValueError:
                    pass
                self.state["history"].append(
                    {"videoId": entry["videoId"],
                     "title": entry.get("title"),
                     "at": time.time()})
                self.state["history"] = self.state["history"][-80:]
            try:
                os.remove(entry["ts"])
            except OSError:
                pass

    # ---------------- state autosave ----------------

    def autosave_loop(self):
        last_push = 0.0
        while self.alive:
            time.sleep(60)
            with self.qlock:
                self.S.save(self.state)
            if time.time() - last_push > 600:
                last_push = time.time()
                if self.S.push_state_branch(ROOT):
                    self.log("state pushed to radio-state branch")

    # ---------------- main ----------------

    def run(self):
        if self.args.song:
            self.request_song(self.args.song, front=True)
        threads = [
            threading.Thread(target=self.downloader_loop, daemon=True),
            threading.Thread(target=self.renderer_loop, daemon=True),
            threading.Thread(target=self.pipeline_loop, daemon=True),
            threading.Thread(target=self.autosave_loop, daemon=True),
        ]
        for t in threads:
            t.start()
        from radio.tgbot import TgBot
        self.tg = TgBot(self)
        self.tg.start()
        try:
            self.tg.reply(self.tg.control,
                          "📻 <b>Radio is ON AIR</b> — Hindi Hits 24/7\n"
                          "Send any song name here and it plays next!")
        except Exception:
            pass
        try:
            while self.alive and time.time() < self.deadline:
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        self.alive = False
        self.log("shutting down — saving state")
        with self.qlock:
            self.S.save(self.state)
        self.S.push_state_branch(ROOT)
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=5.5)
    ap.add_argument("--song", default="")
    ap.add_argument("--rtmp", default=os.environ.get("RTMP_URL", ""))
    ap.add_argument("--key", default=os.environ.get("RTMP_KEY", ""))
    ap.add_argument("--station", default="Hindi Hits Radio")
    ap.add_argument("--clear-stop", action="store_true",
                    help="manual run: clear a previous STOP marker")
    args = ap.parse_args()
    if not (args.rtmp and args.key):
        log("missing RTMP credentials (--rtmp/--key)")
        sys.exit(2)
    import stateio as S
    if args.clear_stop:
        S.clear_stop()
        S.push_state_branch(ROOT)
    if S.stop_requested():
        log("STOP marker present — not starting (chain halted)")
        sys.exit(0)
    st = Station(args)
    st.run()


if __name__ == "__main__":
    main()
