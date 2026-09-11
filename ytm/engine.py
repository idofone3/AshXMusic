"""
Orchestrator: cookie management, search, stream resolution with automatic
browser fallback, and the audio downloader. Thread-safe.
"""
import json
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

import requests

from . import browser as yt_browser
from . import fastdl
from .cookies import parse_cookies, summarize
from .innertube import Innertube, YTApiError, MUSIC_ORIGIN

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:120] or "audio"


class Engine:
    def __init__(self, cookie_path: str, downloads_dir: str):
        self.cookie_path = cookie_path
        self.downloads_dir = downloads_dir
        self.base_dir = os.path.dirname(os.path.abspath(downloads_dir))
        os.makedirs(self.downloads_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.downloads: Dict[str, Dict] = {}
        self._cookie_raw = ""
        self._summary: Dict = {}
        self._tg_threads: List[threading.Thread] = []
        self._load_cookie_file()
        self.prewarm()  # boot the SABR browser while callers search

    # ------------- cookies -------------

    def _load_cookie_file(self):
        if os.path.exists(self.cookie_path):
            with open(self.cookie_path, encoding="utf-8", errors="replace") as f:
                self._cookie_raw = f.read()
        self._summary = summarize(parse_cookies(self._cookie_raw))
        self.it = Innertube(self._cookie_raw)

    def set_cookie(self, raw: str) -> Dict:
        with self._lock:
            self._cookie_raw = raw
            with open(self.cookie_path, "w", encoding="utf-8") as f:
                f.write(raw)
            self.it.set_cookie(raw)
            fastdl.set_browser_cookie(raw)  # browser must reload cookies
            self._summary = summarize(parse_cookies(raw))
            return self._summary

    def cookie_status(self) -> Dict:
        return self._summary

    # ------------- speed helpers -------------

    def prewarm(self):
        """Start the shared SABR browser in the background so the first
        download does not pay the ~10s browser boot cost."""
        if getattr(self, "_prewarmed", False):
            return
        self._prewarmed = True

        def _go():
            try:
                fastdl._SHARED.acquire(self._cookie_raw)
            except Exception:
                pass

        threading.Thread(target=_go, daemon=True).start()

    def wait_telegram(self, timeout: float = 300):
        """Wait for all background Telegram pushes to finish (used by
        batch scripts before printing a final summary)."""
        for t in list(self._tg_threads):
            t.join(max(0.1, timeout))

    # ------------- search -------------

    def search(self, query: str, flt: str = "songs", limit: int = 30) -> List[Dict]:
        with self._lock:
            return self.it.search(query, flt=flt, limit=limit)

    # ------------- resolve -------------

    def resolve(self, video_id: str) -> Dict:
        """Resolve full-length streams, fastest tier first:
        0. direct innertube clients (works on residential IPs)
        1. in-browser multi-client player fetch (yt-dlp style, bot-safe,
           returns plain googlevideo urls -> parallel full-speed download)
        2. legacy web-player format extraction (decipher + in-page probe)
        """
        errors: List[str] = []
        with self._lock:
            try:
                pr, client, streams = self.it.player(video_id)
                track = self.it.track_from_player(pr)
                return {"source": client, "track": track, "streams": streams}
            except Exception as e:
                errors.append(f"innertube: {e}")
        try:
            raw_cookie = self._cookie_raw
            try:
                sig_ts = self.it.signature_timestamp()
            except Exception:
                sig_ts = 0
            r = fastdl.get_direct_streams(video_id, raw_cookie, sig_ts=sig_ts)
            streams = r.get("streams") or []
            if not streams:
                reports = r.get("reports") or []
                detail = ", ".join(f"{x['name']}={x.get('status')}:{x.get('reason','')[:60]}"
                                   for x in reports)
                raise YTApiError(f"no plain urls ({detail})")
            track = Innertube.track_from_player(r.get("player_response") or {})
            return {"source": f"browser:{r.get('client')}",
                    "track": track, "streams": streams}
        except Exception as e:
            errors.append(f"fastdl: {e}")
        try:
            raw_cookie = self._cookie_raw
            r = yt_browser.get_streams_via_browser(
                video_id, raw_cookie, session=self.it.session)
            streams = r.get("streams") or []
            if not streams:
                raise YTApiError("browser found no googlevideo urls")
            track = Innertube.track_from_player(r.get("player_response") or {})
            return {"source": "browser-legacy", "track": track, "streams": streams}
        except Exception as e:
            errors.append(f"browser-legacy: {e}")
        raise YTApiError("resolve failed -> " + " | ".join(errors))

    def pick_stream(self, streams: List[Dict], quality: str = "best",
                    itag: Optional[str] = None) -> Dict:
        if itag:
            for s in streams:
                if s["itag"] == itag:
                    return s
        ranked = sorted(streams, key=lambda s: (s.get("bitrate") or 0), reverse=True)
        q = (quality or "best").lower()
        if q == "low":
            return ranked[-1]
        if q == "medium" and len(ranked) >= 3:
            return ranked[1]
        return ranked[0]

    def direct_url(self, video_id: str, quality: str = "best") -> Dict:
        r = self.resolve(video_id)
        s = self.pick_stream(r["streams"], quality)
        return {"track": r["track"], "source": r["source"], "stream": s}

    # ------------- download -------------

    def _tmp_path(self, tag: str, ext: str) -> str:
        """Unique per-download temp file -> nothing can append/overwrite
        another song's data (fixes the 'previous song in new file' glitch)."""
        return os.path.join(self.downloads_dir,
                            f".tmp_{uuid.uuid4().hex[:8]}_{tag}.{ext}")

    def _finalize_mp4(self, st: Dict, track: Dict, tmp_src: str,
                      video_id: str) -> Optional[Dict]:
        """Convert raw audio -> tagged .mp4 with embedded cover art, then
        push to Telegram if configured. Returns ffprobe info or None on
        failure. Duration is sanity-checked against track metadata."""
        from . import postprocess
        from . import telegram as ytg

        st["status"] = "processing"
        title = track.get("title") or video_id
        artist = track.get("author") or "Unknown"

        thumb = postprocess.fetch_thumbnail(video_id, track.get("thumbnail"))
        base = sanitize_filename(f"{artist} - {title}")
        dest = os.path.join(self.downloads_dir, f"{base} [{video_id}].mp4")
        info = postprocess.to_mp4(tmp_src, dest, title=title, artist=artist,
                                  thumb=thumb)
        st["file"] = dest
        st["bytesTotal"] = st["bytesDone"] = os.path.getsize(dest)
        st["mp4"] = {k: info.get(k) for k in
                     ("duration", "codec", "has_cover", "audioOp", "convertSec")}
        st["filename"] = os.path.basename(dest)

        # sanity: converted duration should match the real song length
        expected = int(track.get("lengthSeconds") or 0)
        dur = float(info.get("duration") or 0)
        if expected and dur:
            ok = abs(dur - expected) <= max(5.0, expected * 0.10)
            st["durationOk"] = ok
            if not ok:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise RuntimeError(
                    f"duration mismatch: got {dur:.1f}s, expected {expected}s "
                    f"(stale/contaminated stream)")

        # push to Telegram in the background — the upload (5-20s) then
        # overlaps with the next song's search/download instead of
        # blocking this worker (batch throughput win)
        st["status"] = "sending"
        st["telegram"] = {"via": "pending"}

        if os.environ.get("YTM_NO_TG") == "1":  # radio mode: no per-song push
            st["telegram"] = {"via": "disabled"}
            st["status"] = "done"
            return info

        def _push():
            try:
                st["telegram"] = ytg.get_logger(self.base_dir).send_song(
                    dest, title=title, artist=artist, thumb=thumb,
                    duration=int(dur or 0), chat_id=st.get("notifyChat"))
            except Exception as e:  # upload errors never fail the download
                st["telegram"] = {"via": "error", "error": str(e)[:200]}

        tp = threading.Thread(target=_push, daemon=True)
        self._tg_threads.append(tp)
        tp.start()
        st["status"] = "done"
        return info

    def start_download(self, video_id: str, quality: str = "best",
                       notify_chat: Optional[int] = None) -> str:
        dl_id = uuid.uuid4().hex[:12]
        self.downloads[dl_id] = {
            "id": dl_id, "videoId": video_id, "status": "queued",
            "bytesDone": 0, "bytesTotal": 0, "file": None, "error": None,
            "notifyChat": notify_chat,
            "startedAt": time.time(),
        }
        t = threading.Thread(target=self._download_worker,
                             args=(dl_id, video_id, quality), daemon=True)
        t.start()
        return dl_id

    def _download_worker(self, dl_id: str, video_id: str, quality: str):
        st = self.downloads[dl_id]
        st["status"] = "resolving"
        st["tierErrors"] = {}

        # ---- tier 1: in-browser SABR (full song in seconds, works
        #      everywhere incl. bot-walled datacenter IPs). YouTube
        #      sometimes answers control-parts only for a burst (~1min);
        #      retry with spacing + a rebuilt browser rides it out. ----
        for tryn in range(3):
            tmp = None
            try:
                if tryn:
                    st["status"] = "resolving"
                    time.sleep(22 * tryn)   # 22s, 44s backoff
                    try:
                        fastdl._SHARED.invalidate()  # fresh PO token
                    except Exception:
                        pass
                st["status"] = "sabr"
                r = fastdl.sabr_download_via_browser(video_id,
                                                     self._cookie_raw)
                if not r.get("complete"):
                    raise YTApiError("sabr download incomplete")
                # metadata captured in-lock by the SABR worker (race-free);
                # only fall back to the shared player if it was unavailable
                track = (r.get("track") if (r.get("track") or {}).get("title")
                         else None) or self._track_from_browser(video_id) or {
                    "videoId": video_id, "title": video_id, "author": "Unknown"}
                st["track"] = track
                st["streamMeta"] = {"itag": r["itag"],
                                    "container": r["container"],
                                    "mimeType": r["mimeType"],
                                    "quality": "sabr-full"}
                st["mode"] = "sabr"
                st["status"] = "downloading"
                st["bytesTotal"] = len(r["data"])
                tmp = self._tmp_path("sabr", r["container"])
                with open(tmp, "wb") as f:
                    f.write(r["data"])
                st["bytesDone"] = len(r["data"])
                self._finalize_mp4(st, track, tmp, video_id)
                return
            except Exception as e:
                st["tierErrors"][f"sabr{tryn + 1}" if tryn else "sabr"] = \
                    str(e)[:300]
            finally:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        # ---- tier 2: direct url (innertube / browser clients) + parallel ----
        tmp = None
        try:
            st["status"] = "resolving"
            info = self.direct_url(video_id, quality)
            track, stream = info["track"], info["stream"]
            st["track"] = track
            st["streamMeta"] = {k: v for k, v in stream.items() if k != "url"}
            st["mode"] = "url"
            st["status"] = "downloading"

            tmp = self._tmp_path("url", stream["container"] + ".part")
            total = int(stream.get("contentLength") or 0)
            st["bytesTotal"] = total

            def _progress(n: int):
                st["bytesDone"] = n

            # yt-dlp-style: parallel Range requests, full speed
            fastdl.parallel_download(stream["url"], tmp, total=total,
                                     progress=_progress, workers=8)
            if os.path.getsize(tmp) <= 0:
                raise RuntimeError("downloaded file is empty")
            self._finalize_mp4(st, track, tmp, video_id)
            return
        except Exception as e:
            st["tierErrors"]["url"] = str(e)[:300]
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        # ---- tier 3: realtime capture (last resort; can be disabled) ----
        if not getattr(self, "allow_capture", True):
            st["status"] = "error"
            st["error"] = "capture tier disabled (fast mode)"
            return
        tmp = None
        try:
            st["status"] = "capturing"
            r = yt_browser.capture_audio_via_browser(video_id, self._cookie_raw)
            if not r.get("data"):
                raise YTApiError("capture produced no audio")
            track = {"videoId": video_id,
                     "title": r.get("title") or video_id,
                     "author": r.get("artist") or "Unknown",
                     "lengthSeconds": int(r.get("durationSec") or 0)}
            st["track"] = track
            st["streamMeta"] = {"itag": "capture", "container": "weba",
                                "quality": "player-default"}
            st["mode"] = "capture"
            st["bytesTotal"] = len(r["data"])
            tmp = self._tmp_path("capture", "webm")
            with open(tmp, "wb") as f:
                f.write(r["data"])
            st["bytesDone"] = len(r["data"])
            self._finalize_mp4(st, track, tmp, video_id)
            return
        except Exception as e:
            st["status"] = "error"
            st["error"] = str(e)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _track_from_browser(self, video_id: str) -> Optional[Dict]:
        """Best-effort track metadata (+thumbnail) from the shared browser."""
        from .browser import _js
        try:
            sb = fastdl._SHARED.acquire(self._cookie_raw)
            with fastdl._SHARED._lock:
                det = _js(sb,
                    "var p=document.getElementById('movie_player');"
                    "if(!(p&&p.getPlayerResponse)) return '{}';"
                    "var vd=(p.getPlayerResponse().videoDetails)||{};"
                    "var th=[];"
                    "try{var a=(vd.thumbnail||{}).thumbnails||[];"
                    "for(var i=0;i<a.length;i++) th.push(a[i].url);}catch(e){}"
                    "return JSON.stringify({title:vd.title,author:vd.author,"
                    "lengthSeconds:parseInt(vd.lengthSeconds||'0'),"
                    "videoId:vd.videoId,thumbnail:th[th.length-1]||null});")
                d = json.loads(det or "{}")
                if d.get("title"):
                    return d
        except Exception:
            pass
        return None

    def list_downloads(self) -> List[Dict]:
        out = []
        for st in self.downloads.values():
            s = {k: v for k, v in st.items() if k != "streamMeta"}
            if st.get("file") and os.path.exists(st["file"]):
                s["sizeBytes"] = os.path.getsize(st["file"])
                s["filename"] = os.path.basename(st["file"])
            out.append(s)
        out.sort(key=lambda x: x.get("startedAt", 0), reverse=True)
        return out
