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
import threading
import time
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from ytm.engine import Engine, YTApiError
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


def _mint_sync(video_id: str, quality: str) -> dict:
    # rung 1: innertube player API only (fast, no shared browser)
    try:
        with engine._lock:
            pr, client, streams = engine.it.player(video_id)
        if streams:
            s = engine.pick_stream(streams, quality)
            if s.get("url"):
                return {"url": s["url"],
                        "stream": {k: v for k, v in s.items() if k != "url"},
                        "track": engine.it.track_from_player(pr),
                        "source": client}
    except Exception:
        pass
    # rung 2: full ladder (innertube -> browser player -> browser legacy)
    info = engine.direct_url(video_id, quality)
    s = info["stream"]
    return {"url": s["url"],
            "stream": {k: v for k, v in s.items() if k != "url"},
            "track": info["track"], "source": info["source"]}


def _prewarm_mints(video_ids: list):
    """Background-mint urls for fresh search results so the first tap on
    play is instant (302 straight to googlevideo). Opportunistic: skipped
    while a download is running (shared browser serves downloads first)."""
    def _run():
        for vid in video_ids[:6]:
            if _mint_get(vid) or not vid:
                continue
            if any(d.get("status") in ("resolving", "sabr", "downloading",
                                       "capturing", "processing", "queued")
                   for d in list(engine.downloads.values())):
                print("[mint] prewarm paused: download in progress", flush=True)
                return
            with _MINT_LOCK:
                if vid in _MINT_BUSY:
                    continue
                _MINT_BUSY.add(vid)
            try:
                _mint_put(vid, _mint_one(vid, "best", budget=60.0))
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
    return {"ok": True, "cookie": engine.cookie_status(), "telegram": tg.status()}


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
    # pre-mint stream urls in the background -> first play is instant
    vids = [r.get("videoId") for r in results if r.get("videoId")][:10]
    if vids:
        _prewarm_mints(vids)
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


@app.get("/stream/{video_id}")
def stream(video_id: str, quality: str = "best", itag: Optional[str] = None,
           redirect: bool = False, mint: bool = False):
    """Audio for browser playback, fastest path first:
      0. cached finished mp4 (instant + seekable)
      1. pre-minted googlevideo url -> 302 (client fetches bytes from its
         own IP: instant, full speed; ?mint=1 returns the url as JSON)
      2. mint on demand (innertube API, ~2-5s) -> 302 / ?mint=1
      3. server-side proxy (only for good networks; fast-fail when the
         datacenter IP is bot-walled/throttled)
    When everything fails clients fall back to POST /downloads/{video_id}
    and play /downloads/file/{dl_id}."""
    if not itag:
        cached = engine.cached_file(video_id)
        if cached:
            return FileResponse(cached, media_type="audio/mp4",
                                filename=os.path.basename(cached))
        m = _mint_get(video_id)
        if not m:
            try:
                m = _mint_one(video_id, quality or "best", budget=28.0)
                _mint_put(video_id, m)
            except Exception as e:
                raise HTTPException(
                    502, f"no stream url: {str(e)[:140]} - "
                         f"use POST /downloads/{video_id} then "
                         f"/downloads/file/{{dlId}}")
        if mint:
            return {"url": m["url"], "source": m.get("source"),
                    "track": m.get("track"),
                    "expiresIn": int(MINT_TTL - (time.time() - m["ts"]))}
        if redirect:
            return RedirectResponse(m["url"])
        try:
            up = requests.get(m["url"], stream=True, timeout=(8, 30),
                              headers={"User-Agent": "Mozilla/5.0",
                                       "Referer": "https://music.youtube.com/"})
            if up.status_code != 200:
                up.close()
                raise RuntimeError(f"origin {up.status_code}")
        except Exception as e:
            raise HTTPException(
                502, f"proxy fetch not viable ({str(e)[:80]}) - "
                     f"use ?redirect=1 or POST /downloads/{video_id}")
        s = m["stream"]
        media_type = s.get("mimeType") or "audio/mp4"
        track = m.get("track") or {}
        filename = (f"{track.get('author', 'Unknown')} - "
                    f"{track.get('title', video_id)}"
                    f".{s.get('container') or 'm4a'}").replace('"', "")
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}
        if "content-length" in up.headers:
            headers["Content-Length"] = up.headers["content-length"]
        return StreamingResponse(up.iter_content(chunk_size=1 << 19),
                                 media_type=media_type, headers=headers)
    # explicit itag: full resolve + exact stream pick (legacy/advanced use)
    try:
        r = engine.resolve(video_id)
    except YTApiError as e:
        raise HTTPException(502, str(e))
    s = engine.pick_stream(r["streams"], "best", itag=itag)
    if not s.get("url"):
        raise HTTPException(502, "picked stream has no plain url")
    if mint:
        return {"url": s["url"], "itag": itag}
    if redirect:
        return RedirectResponse(s["url"])
    up = requests.get(s["url"], stream=True, timeout=(15, 60), headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://music.youtube.com/"})
    media_type = s.get("mimeType") or "audio/mp4"
    filename = (f"{r['track'].get('author','Unknown')} - "
                f"{r['track'].get('title', video_id)}.{s['container']}").replace('"', "")
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    if "content-length" in up.headers:
        headers["Content-Length"] = up.headers["content-length"]
    return StreamingResponse(up.iter_content(chunk_size=1 << 19),
                             media_type=media_type, headers=headers)


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
