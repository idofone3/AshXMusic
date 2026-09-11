"""
YTMusic Download API — FastAPI wrapper around the pure-python ytm engine.

Run:  python3 run.py          (serves on 0.0.0.0:8000)
Docs: http://localhost:8000/docs
"""
import os
import threading
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from ytm.engine import Engine, YTApiError
from ytm.telegram import get_logger as tg_logger

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE_PATH = os.path.join(BASE, "cookies.txt")
DOWNLOADS_DIR = os.path.join(BASE, "downloads")

app = FastAPI(title="YTMusic Download API", version="2.0.0",
              description="Personal YouTube Music search/stream/download API "
                          "(pure python + seleniumbase, no premade YT libs). "
                          "All downloads are mp4 with embedded cover art; "
                          "optional Telegram auto-push.")
engine = Engine(COOKIE_PATH, DOWNLOADS_DIR)
tg = tg_logger(BASE)


@app.on_event("startup")
def _start_bot():
    """Background Telegram bot: long-polls /get commands whenever a bot
    token + chat are configured (data/telegram.json or env)."""
    from ytm.telegram_bot import get_bot
    started = get_bot(BASE, engine).start()
    print(f"[server] telegram bot {'started' if started else 'not configured'}",
          flush=True)


@app.get("/bot")
def bot_status():
    from ytm.telegram_bot import get_bot
    return get_bot(BASE, engine).status()


class CookieBody(BaseModel):
    raw: str


class TelegramBody(BaseModel):
    bot_token: str
    chat_id: str
    enabled: Optional[bool] = True


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
        return {"query": q, "filter": filter,
                "results": engine.search(q, flt=filter, limit=limit)}
    except Exception as e:
        raise HTTPException(502, str(e))


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
           redirect: bool = False):
    """Proxy the audio stream (or 302-redirect straight to googlevideo)."""
    try:
        info = engine.direct_url(video_id, quality or "best")
    except YTApiError as e:
        raise HTTPException(502, str(e))
    s = info["stream"]
    if itag:
        r = engine.resolve(video_id)
        s = engine.pick_stream(r["streams"], "best", itag=itag)
    if redirect:
        return RedirectResponse(s["url"])
    up = requests.get(s["url"], stream=True, timeout=(15, 60), headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://music.youtube.com/"})
    media_type = s.get("mimeType") or "audio/mp4"
    filename = (f"{info['track'].get('author','Unknown')} - "
                f"{info['track'].get('title', video_id)}.{s['container']}").replace('"', "")
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
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
