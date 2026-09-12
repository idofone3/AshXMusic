"""
AshXMusic API — FastAPI wrapper around the pure-python ytm engine.

Run:  python3 run.py          (serves on 0.0.0.0:${PORT:-8000})
Docs: http://localhost:8000/docs

Environment (all optional):
  PORT                 listen port (Render sets it; default 8000)
  YTM_DL_DIR           downloads dir (default ./downloads; use /tmp/... on
                       ephemeral hosts like the Render free tier — no disk)
  COOKIES_B64          base64 of cookies.txt, used when cookies.txt missing
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
                       seed the telegram auto-push config when nothing is
                       saved yet (persisted to data/telegram.json)
  YTM_NO_TG=1          disable per-download telegram pushes entirely
"""
import base64
import concurrent.futures
import os
import re
import threading
import time
import urllib.parse
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from pydantic import BaseModel

from ytm import fastdl
from ytm.engine import Engine, YTApiError, sanitize_filename
from ytm.innertube import Innertube
from ytm.telegram import get_logger as tg_logger

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE_PATH = os.path.join(BASE, "cookies.txt")
DOWNLOADS_DIR = os.environ.get("YTM_DL_DIR") or os.path.join(BASE, "downloads")
STATIC_DIR = os.path.join(BASE, "static")

# ephemeral hosts (Render free tier has no disks): seed cookies from env
if not os.path.exists(COOKIE_PATH) and os.environ.get("COOKIES_B64"):
    try:
        with open(COOKIE_PATH, "wb") as _f:
            _f.write(base64.b64decode(os.environ["COOKIES_B64"]))
        print("[server] cookies restored from COOKIES_B64", flush=True)
    except Exception as _e:
        print(f"[server] COOKIES_B64 decode failed: {_e}", flush=True)

app = FastAPI(title="AshXMusic API", version="3.0.0",
              description="YouTube Music search/stream/download API "
                          "(pure python + seleniumbase, no premade YT libs). "
                          "All downloads are mp4 with embedded cover art; "
                          "optional Telegram auto-push.")
engine = Engine(COOKIE_PATH, DOWNLOADS_DIR)
tg = tg_logger(BASE)

# ------------- instant-play mint cache -------------
# Server only MINTS googlevideo urls (fast innertube API calls, no media
# bytes); the CLIENT fetches the bytes directly from googlevideo with its
# own (residential) IP -> full speed, instant playback. Media bytes pulled
# from datacenter IPs (Actions/Render) are throttled to a crawl by Google,
# so the server never proxies media unless explicitly asked.
_MINT: dict = {}                 # videoId -> {url, stream, track, source, ts}
_MINT_LOCK = threading.Lock()
_MINT_BUSY: set = set()
MINT_TTL = 3 * 3600              # googlevideo urls live ~6h; refresh at 3h


def _mint_get(video_id: str) -> Optional[dict]:
    with _MINT_LOCK:
        m = _MINT.get(video_id)
        if m and time.time() - m["ts"] < MINT_TTL:
            return m
        if m:
            _MINT.pop(video_id, None)
    return None


def _mint_put(video_id: str, m: dict):
    m["ts"] = time.time()
    with _MINT_LOCK:
        _MINT[video_id] = m


def _mint_one(video_id: str, quality: str = "best", budget: float = 28.0) -> dict:
    """Resolve a plain stream url WITHOUT touching media bytes.
    Rung 1: pure innertube player API (no browser -> never contends with
    SABR workers). Rung 2: full resolve ladder (browser-assisted).
    Thread-budgeted: raises within ~`budget` seconds."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_mint_sync, video_id, quality)
        try:
            return fut.result(timeout=budget)
        except concurrent.futures.TimeoutError:
            raise RuntimeError(f"mint budget {budget:.0f}s exceeded")
    finally:
        ex.shutdown(wait=False)


_MINT_IT: Optional[Innertube] = None      # dedicated session for mints


def _mint_innertube() -> Innertube:
    """Own Innertube session for minting -> pure API calls that never take
    engine._lock, so /search and /health stay snappy while mints run."""
    global _MINT_IT
    if _MINT_IT is None:
        _MINT_IT = Innertube(engine._cookie_raw)
    return _MINT_IT


def _mint_sync(video_id: str, quality: str) -> dict:
    """Rung 1: innertube player API (dedicated lock-free session).
    Rung 2: full resolve ladder. NOTE: SABR-captured playback urls are
    UMP-framed (application/vnd.yt-ump) and unplayable by plain <audio> —
    never hand those to clients; for instant playback we pre-DOWNLOAD
    search results instead (see _prewarm_top)."""
    try:
        it = _mint_innertube()
        pr, client, streams = it.player(video_id)
        if streams:
            s = engine.pick_stream(streams, quality)
            if s.get("url"):
                return {"url": s["url"],
                        "stream": {k: v for k, v in s.items() if k != "url"},
                        "track": it.track_from_player(pr),
                        "source": client}
    except Exception:
        pass
    info = engine.direct_url(video_id, quality)
    s = info["stream"]
    return {"url": s["url"],
            "stream": {k: v for k, v in s.items() if k != "url"},
            "track": info["track"], "source": info["source"]}


_ACTIVE_STATES = ("queued", "resolving", "sabr", "downloading",
                  "capturing", "processing", "sending")


def _active_download(video_id: str) -> Optional[str]:
    for d in engine.downloads.values():
        if d.get("videoId") == video_id and d.get("status") in _ACTIVE_STATES:
            return d["id"]
    return None


def _prewarm_download(video_id: str) -> Optional[str]:
    """Guarantee a download is running (or finished) for video_id.
    Returns the download id or None when already cached."""
    if engine.cached_file(video_id):
        return None
    return _active_download(video_id) or engine.start_download(video_id, "best")


def _prewarm_top(results: list):
    """Background-download the top search results -> by the time the user
    taps play the song is cached and playback is INSTANT (206 seekable)."""
    vids = [r.get("videoId") for r in results if r.get("videoId")][:5]

    def _run():
        for vid in vids:
            if _active_download(vid) or engine.cached_file(vid):
                continue
            busy = sum(1 for d in engine.downloads.values()
                       if d.get("status") in _ACTIVE_STATES)
            if busy >= 3:
                return
            try:
                engine.start_download(vid, "best")
                print(f"[prewarm] downloading {vid}", flush=True)
            except Exception as e:
                print(f"[prewarm] {vid}: {str(e)[:80]}", flush=True)
            time.sleep(2)
    threading.Thread(target=_run, daemon=True).start()


# ------------- instant SABR streaming (see fastdl.sabr_stream) -------------

# googlevideo media fetches from this process use a real, current Chrome UA
_UA_GV = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

_FINALIZING: set = set()          # videoIds with a tee-finalize in flight


def _warm_pot(video_id: str):
    """Pre-mint the pot-carrying SABR url so the next /stream TTFB ~ 0."""
    try:
        fastdl.pot_url(video_id, engine._cookie_raw)
        print(f"[pot] warm {video_id}", flush=True)
    except Exception as e:
        print(f"[pot] warm {video_id} failed: {str(e)[:100]}", flush=True)


def _parse_range(range_header: Optional[str]):
    """'bytes=a-b' -> (a, b|None); 'bytes=-n' -> ('suffix', n); else None."""
    if not range_header:
        return None
    m = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):
        return ("suffix", int(m.group(2)))
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None)


def _safe_name(track: dict, video_id: str) -> str:
    t = track or {}
    return sanitize_filename(
        f"{t.get('author') or 'Unknown'} - {t.get('title') or video_id}")


def _tee_finalize(video_id: str, tee_path: str, track: dict):
    """Turn a fully-streamed play into a tagged, cover-art mp4 cache entry
    (background; best-effort). After this the song plays back from disk
    instantly + seekable forever."""
    if video_id in _FINALIZING:
        return
    _FINALIZING.add(video_id)
    try:
        if engine.cached_file(video_id):
            try:
                os.remove(tee_path)
            except OSError:
                pass
            return
        if (not os.path.exists(tee_path)
                or os.path.getsize(tee_path) < 4096):
            return
        from ytm import postprocess
        title = track.get("title") or video_id
        artist = track.get("author") or "Unknown"
        thumb = postprocess.fetch_thumbnail(video_id, track.get("thumbnail"))
        base = sanitize_filename(f"{artist} - {title}")
        dest = os.path.join(DOWNLOADS_DIR, f"{base} [{video_id}].mp4")
        postprocess.to_mp4(tee_path, dest, title=title, artist=artist,
                           thumb=thumb)
        print(f"[tee] cached {video_id} -> {os.path.basename(dest)}",
              flush=True)
    except Exception as e:
        print(f"[tee] finalize {video_id} failed: {str(e)[:120]}", flush=True)
    finally:
        try:
            os.remove(tee_path)
        except OSError:
            pass
        _FINALIZING.discard(video_id)


def _pump_response(video_id: str, range_header: Optional[str]):
    """Instant-play response: stream CLEAN audio bytes through the
    pot-carrying SABR url (browser-fetched windows, UMP stripped).
    Declares exact Content-Length -> <audio> gets full seeking; every byte
    is tee'd to disk so a completed play caches the song for replays."""
    m = fastdl.pot_url(video_id, engine._cookie_raw)
    url = m["url"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    clen = int((q.get("clen") or ["0"])[0])
    if not clen:
        raise RuntimeError("sabr url missing clen")
    mime = m.get("mime") or "audio/mp4"
    ctype = "audio/webm" if ("webm" in mime or "opus" in mime) else "audio/mp4"

    start, end, status = 0, clen - 1, 200
    rng = _parse_range(range_header)
    if rng:
        if rng[0] == "suffix":
            start = max(0, clen - int(rng[1] or 0))
        else:
            start = min(int(rng[0]), clen - 1)
            if rng[1] is not None:
                end = min(int(rng[1]), clen - 1)
        status = 206
    length = end - start + 1

    track = m.get("track") or {}
    fname = _safe_name(track, video_id) + (".webm" if ctype == "audio/webm"
                                           else ".m4a")

    tee = None
    tee_path = None
    if start == 0 and status == 200 and not engine.cached_file(video_id):
        tee_path = os.path.join(DOWNLOADS_DIR, f".tee_{video_id}.part")
        try:
            tee = open(tee_path, "wb")
        except OSError:
            tee = None

    def gen():
        sent = 0
        try:
            for chunk in fastdl.sabr_stream(video_id, engine._cookie_raw,
                                            start=start, end=end):
                if tee:
                    try:
                        tee.write(chunk)
                    except Exception:
                        pass
                sent += len(chunk)
                yield chunk
        finally:
            if tee:
                try:
                    tee.close()
                except Exception:
                    pass
                if sent >= clen - 4096:
                    threading.Thread(target=_tee_finalize,
                                     args=(video_id, tee_path, dict(track)),
                                     daemon=True).start()

    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{fname}"',
        "Cache-Control": "no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{clen}"
    return StreamingResponse(gen(), media_type=ctype, status_code=status,
                             headers=headers)


def _plain_proxy_response(video_id: str, range_header: Optional[str],
                          quality: str = "best"):
    """Last-resort fallback: proxy a minted plain googlevideo url (works
    when the network is not bot-walled). Range requests pass through."""
    m = _mint_get(video_id)
    if not m:
        m = _mint_one(video_id, quality, budget=20.0)
        _mint_put(video_id, m)
    fwd = {"User-Agent": _UA_GV, "Referer": "https://music.youtube.com/",
           "Origin": "https://music.youtube.com"}
    if range_header:
        fwd["Range"] = range_header
    up = requests.get(m["url"], stream=True, timeout=(6, 30), headers=fwd)
    if up.status_code not in (200, 206):
        up.close()
        raise RuntimeError(f"origin {up.status_code}")
    s = m.get("stream") or {}
    media_type = s.get("mimeType") or "audio/mp4"
    fname = _safe_name(m.get("track") or {}, video_id) + "." + (
        s.get("container") or "m4a")
    headers = {"Content-Disposition": f'inline; filename="{fname}"',
               "Cache-Control": "no-store"}
    for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if up.headers.get(h):
            headers[h] = up.headers[h]
    return StreamingResponse(up.iter_content(chunk_size=1 << 19),
                             media_type=media_type,
                             status_code=up.status_code, headers=headers)


def _mint_bg_start(video_id: str, budget: float = 110.0) -> bool:
    """Kick off a background mint for one video (False if already running
    or already cached). Never blocks the caller."""
    if _mint_get(video_id):
        return False
    with _MINT_LOCK:
        if video_id in _MINT_BUSY:
            return False
        _MINT_BUSY.add(video_id)

    def _run():
        try:
            _mint_put(video_id, _mint_one(video_id, "best", budget=budget))
            print(f"[mint] ready {video_id}", flush=True)
        except Exception as e:
            print(f"[mint] failed {video_id}: {str(e)[:100]}", flush=True)
        finally:
            with _MINT_LOCK:
                _MINT_BUSY.discard(video_id)
    threading.Thread(target=_run, daemon=True).start()
    return True


def _prewarm_mints(video_ids: list):
    """Background-mint urls for fresh search results so the first tap on
    play is instant (302 straight to googlevideo). Opportunistic: skipped
    while a download is running (shared browser serves downloads first)."""
    def _run():
        for vid in video_ids[:6]:
            waited = 0
            # wait out active downloads instead of dropping the prewarm
            while waited < 600 and any(
                    d.get("status") in ("resolving", "sabr", "downloading",
                                        "capturing", "processing", "queued")
                    for d in list(engine.downloads.values())):
                time.sleep(15)
                waited += 15
            if _mint_get(vid) or not vid:
                continue
            with _MINT_LOCK:
                if vid in _MINT_BUSY:
                    continue
                _MINT_BUSY.add(vid)
            try:
                _mint_put(vid, _mint_one(vid, "best", budget=110.0))
                print(f"[mint] prewarmed {vid}", flush=True)
            except Exception as e:
                print(f"[mint] prewarm {vid} failed: {str(e)[:100]}", flush=True)
            finally:
                with _MINT_LOCK:
                    _MINT_BUSY.discard(vid)
    threading.Thread(target=_run, daemon=True).start()

# seed the telegram push config from env when nothing is saved yet
if (not tg.status()["configured"] and os.environ.get("TELEGRAM_BOT_TOKEN")
        and os.environ.get("TELEGRAM_CHAT_ID")):
    try:
        tg.set(os.environ["TELEGRAM_BOT_TOKEN"],
               os.environ["TELEGRAM_CHAT_ID"], True)
        print("[server] telegram push configured from env", flush=True)
    except Exception as _e:
        print(f"[server] telegram env seeding failed: {_e}", flush=True)


class CookieBody(BaseModel):
    raw: str


class TelegramBody(BaseModel):
    bot_token: str
    chat_id: str
    enabled: Optional[bool] = True


@app.get("/", include_in_schema=False)
def home():
    """Minimal dark web UI: search, play, download."""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    n_cache = 0
    try:
        n_cache = sum(1 for f in os.listdir(DOWNLOADS_DIR)
                      if f.endswith(".mp4") and not f.startswith("."))
    except OSError:
        pass
    return {"ok": True, "cookie": engine.cookie_status(), "telegram": tg.status(),
            "cache": n_cache}


# ------------- telegram log -------------


@app.post("/telegram")
def set_telegram(body: TelegramBody):
    """Configure the Telegram song log (bot token + chat id), persisted."""
    st = tg.set(body.bot_token, body.chat_id, body.enabled)
    if st["configured"]:
        test = tg.send_test()
        if not test.get("ok"):
            return {"ok": True, "saved": True, "status": st,
                    "test": test,
                    "note": "saved, but the test message failed - check token/chat_id"}
        return {"ok": True, "saved": True, "status": st, "test": test}
    raise HTTPException(400, "bot_token and chat_id are required")


@app.get("/telegram")
def telegram_status():
    return tg.status()


@app.post("/telegram/test")
def telegram_test():
    r = tg.send_test()
    if not r.get("ok"):
        raise HTTPException(400, f"telegram test failed: {r.get('description', r)}")
    return {"ok": True, "result": r}


@app.post("/cookies")
def set_cookies(body: CookieBody):
    """Update the YouTube Music cookie (any format: netscape / json / header)."""
    s = engine.set_cookie(body.raw)
    if not s["logged_in"]:
        raise HTTPException(400, f"cookie parsed but no login session found: {s}")
    return {"ok": True, "cookie": s}


@app.get("/search")
def search(q: str = Query(..., min_length=1),
           filter: str = Query("songs", pattern="^(all|songs|videos|albums|artists|playlists)$"),
           limit: int = Query(20, ge=1, le=100)):
    try:
        results = engine.search(q, flt=filter, limit=limit)
    except Exception as e:
        raise HTTPException(502, str(e))
    # pre-download top results in the background -> first tap = instant
    _prewarm_top(results)
    return {"query": q, "filter": filter, "results": results}


@app.get("/track/{video_id}")
def track(video_id: str, quality: str = "best"):
    try:
        r = engine.resolve(video_id)
    except YTApiError as e:
        raise HTTPException(502, str(e))
    chosen = engine.pick_stream(r["streams"], quality)
    return {
        "source": r["source"],
        "track": r["track"],
        "streams": [{k: v for k, v in s.items() if k != "url"} for s in r["streams"]],
        "chosen": {k: v for k, v in chosen.items() if k != "url"},
    }


@app.get("/play/{video_id}")
def play_meta(video_id: str):
    """One tiny JSON call for the web UI -> how to play this song instantly:
      file        cached mp4 exists -> GET /stream/{id} (instant + seekable)
      downloading a full-quality download is already running -> poll
                  statusUrl and play when done (usually a few seconds)
      stream      nothing ready -> GET /stream/{id} (instant SABR stream,
                  pot url is pre-minted in the background right now)
    Every response is immediate; nothing here ever blocks on media bytes."""
    cached = engine.cached_file(video_id)
    if cached:
        return {"mode": "file", "url": f"/stream/{video_id}",
                "file": os.path.basename(cached)}
    dl = _active_download(video_id)
    if dl:
        return {"mode": "downloading", "url": f"/stream/{video_id}",
                "downloadId": dl, "statusUrl": f"/downloads/{dl}"}
    threading.Thread(target=_warm_pot, args=(video_id,), daemon=True).start()
    return {"mode": "stream", "url": f"/stream/{video_id}"}


@app.get("/stream/{video_id}")
def stream(video_id: str, request: Request, quality: str = "best",
           itag: Optional[str] = None, redirect: bool = False,
           mint: bool = False):
    """Audio for browser playback (and any player), fastest path first:
      0. cached mp4 (instant + byte-range seekable)
      1. SABR pump — clean audio streamed through the trusted-PO-token
         browser session (full speed on datacenter IPs, exact
         Content-Length, Range supported); a completed play also caches
         the tagged mp4 for future instant replays
      2. plain googlevideo proxy (minted url, Range passthrough)
    ?redirect=1 -> 302 to a minted plain url (advanced).  ?mint=1 -> the
    /play/{id} JSON meta.  Explicit ?itag= -> legacy exact-stream pick."""
    if mint:
        return play_meta(video_id)
    cached = engine.cached_file(video_id)
    if cached:
        return FileResponse(cached, media_type="audio/mp4", headers={
            "Content-Disposition":
                f'inline; filename="{os.path.basename(cached)}"',
            "Cache-Control": "no-store"})
    range_header = request.headers.get("range")
    if itag:
        # explicit itag: full resolve + exact stream pick (legacy/advanced)
        try:
            r = engine.resolve(video_id)
        except YTApiError as e:
            raise HTTPException(502, str(e))
        s = engine.pick_stream(r["streams"], "best", itag=itag)
        if not s.get("url"):
            raise HTTPException(502, "picked stream has no plain url")
        if redirect:
            return RedirectResponse(s["url"])
        up = requests.get(s["url"], stream=True, timeout=(15, 60), headers={
            "User-Agent": _UA_GV,
            "Referer": "https://music.youtube.com/"})
        media_type = s.get("mimeType") or "audio/mp4"
        filename = (f"{r['track'].get('author','Unknown')} - "
                    f"{r['track'].get('title', video_id)}"
                    f".{s['container']}").replace('"', "")
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}
        if up.headers.get("content-length"):
            headers["Content-Length"] = up.headers["content-length"]
        return StreamingResponse(up.iter_content(chunk_size=1 << 19),
                                 media_type=media_type, headers=headers)
    if redirect:
        m = _mint_get(video_id)
        if not m:
            try:
                m = _mint_one(video_id, quality or "best", budget=25.0)
                _mint_put(video_id, m)
            except Exception as e:
                raise HTTPException(502, f"no stream url: {str(e)[:140]}")
        return RedirectResponse(m["url"])
    # 1. instant SABR pump (primary)
    pump_err = None
    try:
        return _pump_response(video_id, range_header)
    except HTTPException:
        raise
    except Exception as e:
        pump_err = str(e)
        print(f"[stream] pump {video_id} failed: {pump_err[:120]}", flush=True)
    # 2. plain googlevideo proxy (fallback)
    try:
        return _plain_proxy_response(video_id, range_header, quality)
    except Exception as e:
        raise HTTPException(
            502, f"stream unavailable (pump: {(pump_err or '')[:90]} | "
                 f"proxy: {str(e)[:90]}) - use /dl/{video_id}")


@app.get("/dl/{video_id}")
def dl_direct(video_id: str, fmt: str = "mp4", quality: str = "best"):
    """Direct, wget-able download URL (no polling dance):
      GET /dl/{videoId}          -> tagged mp4, cover art embedded
      GET /dl/{videoId}?fmt=mp3  -> 320 kbps mp3, cover art embedded
    If the song is not cached yet this waits while it is fetched at full
    speed (SABR, usually well under 15s), then serves the file. Also
    fires the Telegram push exactly like POST /downloads/{id}."""
    fmt = (fmt or "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        raise HTTPException(400, "fmt must be mp4 or mp3")
    file = engine.cached_file(video_id)
    if not file:
        try:
            dl_id = _prewarm_download(video_id)
        except Exception as e:
            raise HTTPException(502, f"download start failed: {str(e)[:140]}")
        if dl_id:
            deadline = time.time() + 180
            while time.time() < deadline:
                st = engine.downloads.get(dl_id) or {}
                sst = st.get("status")
                if sst == "done":
                    file = st.get("file") or engine.cached_file(video_id)
                    break
                if sst == "error":
                    raise HTTPException(
                        502, f"download failed: {(st.get('error') or '')[:140]}")
                time.sleep(1.0)
            if not file:
                raise HTTPException(504, "still downloading - retry shortly")
        else:
            file = engine.cached_file(video_id)
    if not file or not os.path.exists(file):
        raise HTTPException(502, "no file available")

    if fmt == "mp3":
        from ytm import postprocess
        if not postprocess.available():
            raise HTTPException(501, "mp3 conversion unavailable on this host")
        mp3 = os.path.join(DOWNLOADS_DIR,
                           os.path.basename(file)[:-4] + ".mp3")
        if not (os.path.exists(mp3) and os.path.getsize(mp3) > 4096):
            base = os.path.basename(file)[:-4]
            if " - " in base:
                artist, title = base.split(" - ", 1)
            else:
                artist, title = "Unknown", base
            thumb = postprocess.fetch_thumbnail(video_id)
            postprocess.to_mp3(file, mp3, title=title, artist=artist,
                               thumb=thumb)
        file = mp3

    media = "audio/mpeg" if fmt == "mp3" else "audio/mp4"
    return FileResponse(file, media_type=media, headers={
        "Content-Disposition":
            f'attachment; filename="{os.path.basename(file)}"',
        "Cache-Control": "no-store"})


@app.post("/downloads/{video_id}")
def start_download(video_id: str, quality: str = "best",
                   notify_chat: Optional[str] = None):
    """notify_chat: optional Telegram chat that receives the finished song
    (used by the /get bot); defaults to the configured telegram chat."""
    dl_id = engine.start_download(video_id, quality,
                                  notify_chat=notify_chat)
    return {"downloadId": dl_id, "statusUrl": f"/downloads/{dl_id}"}


@app.get("/downloads")
def list_downloads():
    return {"downloads": engine.list_downloads()}


@app.get("/downloads/{dl_id}")
def download_status(dl_id: str):
    st = engine.downloads.get(dl_id)
    if not st:
        raise HTTPException(404, "unknown download id")
    return {k: v for k, v in st.items() if k != "streamMeta"}


@app.get("/downloads/file/{dl_id}")
def download_file(dl_id: str):
    st = engine.downloads.get(dl_id)
    if not st or not st.get("file") or not os.path.exists(st["file"]):
        raise HTTPException(404, "file not ready")
    return FileResponse(st["file"], filename=os.path.basename(st["file"]),
                        media_type="audio/mp4")
