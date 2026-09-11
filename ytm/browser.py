"""
SeleniumBase (UC mode) + Xvfb fallback.

Strategy (YouTube now bot-walls every innertube player client from
datacenter IPs, and the web player streams via SABR — neither yields
plain downloadable URLs):

  1. Real Chrome (UC mode) with the user's cookies opens the YT Music
     watch page — this passes every bot check.
  2. The page's own player response is pulled via
     movie_player.getPlayerResponse() — it contains the classic
     adaptive formats (itag/mime/length + signatureCipher).
  3. The `s` params are deciphered in Python (ytm.decipher) using the
     player base.js referenced by music.youtube.com.
  4. Each deciphered audio URL is probed *from inside the same browser*
     (Range: bytes=0-1023) and only HTTP 200/206 survivors are returned.
"""
import json
import os
import re
import time
from typing import List, Optional
from urllib.parse import parse_qsl

import requests

from .cookies import parse_cookies

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ITAG_MAP = {
    "140": ("m4a", "mp4a.40.2"), "141": ("m4a", "mp4a.40.2"),
    "139": ("m4a", "mp4a.40.5"), "256": ("m4a", "mp4a.40.5"),
    "258": ("m4a", "mp4a.40.5"), "599": ("m4a", "mp4a.40.5"),
    "251": ("weba", "opus"), "250": ("weba", "opus"), "249": ("weba", "opus"),
}

_FMT_SCRIPT = (
    "var p=document.getElementById('movie_player');"
    "if(!(p&&p.getPlayerResponse)) return '[]';"
    "var pr=p.getPlayerResponse();"
    "var sd=pr&&pr.streamingData?pr.streamingData:{};"
    "var f=(sd.adaptiveFormats||[]).concat(sd.formats||[]);"
    "var out=[];"
    "for(var i=0;i<f.length;i++){"
    "  var x=f[i];"
    "  out.push({itag:String(x.itag),mimeType:x.mimeType||'',"
    "    bitrate:x.bitrate||0,contentLength:x.contentLength||0,"
    "    approxDurationMs:x.approxDurationMs||0,"
    "    audioQuality:x.audioQuality||'',"
    "    url:x.url||'',signatureCipher:x.signatureCipher||''});"
    "}"
    "return JSON.stringify(out);"
)

_PROBE_SCRIPT = (
    "var urls = arguments[0];"
    "var done = arguments[arguments.length - 1];"
    "Promise.all(urls.map(function(u){"
    "  return fetch(u, {headers:{Range:'bytes=0-1023'}})"
    "    .then(function(r){ return {u:u, s:r.status}; })"
    "    .catch(function(e){ return {u:u, s:0}; });"
    "})).then(function(rs){ done(JSON.stringify(rs)); });"
)


def _js(sb, script: str, *args):
    """execute_script with UC reconnect/retry handling."""
    last_err: Optional[Exception] = None
    for _ in range(4):
        try:
            if args:
                return sb.execute_script(script, *args)
            return sb.execute_script(script)
        except Exception as e:  # connection refused during UC reconnect
            last_err = e
            time.sleep(2)
            try:
                sb.reconnect()
            except Exception:
                pass
    raise last_err if last_err else RuntimeError("js call failed")


def _js_async(sb, script: str, *args):
    last_err: Optional[Exception] = None
    for _ in range(4):
        try:
            sb.driver.set_script_timeout(45)
            # selenium 4 signature is variadic: each arg spreads into
            # in-page `arguments`; the done-callback is appended last
            return sb.driver.execute_async_script(script, *args)
        except Exception as e:
            last_err = e
            time.sleep(2)
            try:
                sb.reconnect()
            except Exception:
                pass
    raise last_err if last_err else RuntimeError("async js call failed")


def fetch_player_js(session: requests.Session) -> str:
    r = session.get("https://music.youtube.com/?hl=en", timeout=25)
    r.raise_for_status()
    m = (re.search(r'"PLAYER_JS_URL"\s*:\s*"([^"]+)"', r.text)
         or re.search(r'"(/s/player/[A-Za-z0-9_-]+/(?:player_es6|www-player|base)\.?[A-Za-z0-9_]*)"', r.text)
         or re.search(r'"jsUrl"\s*:\s*"(/s/player/[^"]+)"', r.text))
    if not m:
        raise RuntimeError("player base.js url not found")
    resp = session.get("https://music.youtube.com" + m.group(1), timeout=30)
    resp.raise_for_status()
    return resp.text


def get_streams_via_browser(video_id: str, raw_cookie: str,
                            wait_seconds: int = 60,
                            session: Optional[requests.Session] = None) -> dict:
    from seleniumbase import SB  # lazy import
    from .decipher import make_decipher

    formats: List[dict] = []
    perf_urls: List[str] = []
    player_details: dict = {}

    with SB(uc=True, xvfb=True, locale_code="en", disable_csp=True,
            maximize=True, chromium_arg="--autoplay-policy=no-user-gesture-required") as sb:
        sb.open("https://music.youtube.com/?hl=en")
        sb.sleep(2)
        for c in parse_cookies(raw_cookie):
            domain = (c.get("domain") or ".youtube.com").lstrip(".")
            if "youtube" not in domain:
                continue
            ck = {"name": c["name"], "value": c["value"],
                  "domain": "." + domain.lstrip("."), "path": c.get("path") or "/"}
            try:
                sb.driver.add_cookie(ck)
            except Exception:
                pass
        sb.uc_open_with_reconnect(
            f"https://music.youtube.com/watch?v={video_id}&hl=en",
            reconnect_time=5)
        try:
            sb.reconnect()
        except Exception:
            pass
        sb.sleep(5)

        _js(sb, "var p=document.getElementById('movie_player');"
                "if(p){try{p.mute();p.playVideo();}catch(e){}}")

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                formats = json.loads(_js(sb, _FMT_SCRIPT) or "[]")
            except Exception:
                formats = []
            has_audio = any(f["mimeType"].startswith("audio") and
                            (f["url"] or f["signatureCipher"]) for f in formats)
            if has_audio:
                break
            sb.sleep(3)
            try:
                _js(sb, "var p=document.getElementById('movie_player');"
                        "if(p){try{p.playVideo();}catch(e){}}")
            except Exception:
                pass

        try:
            perf_urls = _js(sb,
                "return performance.getEntriesByType('resource')"
                ".map(function(e){return e.name;})"
                ".filter(function(n){return n.indexOf('googlevideo')!==-1;})") or []
        except Exception:
            perf_urls = []

        try:
            det = _js(sb,
                "var p=document.getElementById('movie_player');"
                "if(!(p&&p.getPlayerResponse)) return '{}';"
                "var pr=p.getPlayerResponse();"
                "return JSON.stringify((pr&&pr.videoDetails)||{});")
            player_details = json.loads(det or "{}")
        except Exception:
            player_details = {}

        # ---- build candidate audio URLs ----
        pending: List[dict] = []
        for f in formats:
            mime = f["mimeType"] or ""
            if not mime.startswith("audio"):
                continue
            if f["url"]:
                pending.append({**f, "_url": f["url"]})
            elif f["signatureCipher"]:
                d = dict(parse_qsl(f["signatureCipher"], keep_blank_values=True))
                base_url, s_param = d.get("url"), d.get("s")
                sp = d.get("sp", "sig")
                if base_url and s_param:
                    pending.append({**f, "_url": base_url,
                                    "_s": s_param, "_sp": sp})

        if any("_s" in p for p in pending):
            try:
                sess = session or requests.Session()
                if not sess.headers.get("User-Agent"):
                    sess.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; "
                                                  "Win64; x64) AppleWebKit/537.36")
                dec = make_decipher(fetch_player_js(sess))
                for p in pending:
                    if "_s" in p:
                        try:
                            p["_url"] = (p["_url"] + "&" +
                                         p.get("_sp", "sig") + "=" +
                                         dec(p["_s"]))
                        except Exception:
                            p["_url"] = None
            except Exception:
                pass

        # ---- probe candidates inside the same browser ----
        candidates = [p for p in pending if p.get("_url")]
        statuses: dict = {}
        if candidates:
            try:
                raw = _js_async(sb, _PROBE_SCRIPT, [p["_url"] for p in candidates])
                statuses = {r["u"]: r["s"] for r in json.loads(raw or "[]")}
            except Exception:
                statuses = {}

    streams = []
    for p in candidates:
        if statuses.get(p["_url"]) not in (200, 206):
            continue
        itag = p["itag"]
        container, codec = ITAG_MAP.get(itag, ("m4a", ""))
        if "webm" in p["mimeType"] or "opus" in p["mimeType"]:
            container, codec = "weba", "opus"
        streams.append({
            "itag": itag, "container": container, "codec": codec,
            "mimeType": p["mimeType"].split(";")[0],
            "bitrate": int(p["bitrate"] or 0),
            "quality": (p["audioQuality"] or "").replace("AUDIO_QUALITY_", ""),
            "contentLength": int(p["contentLength"] or 0),
            "durationSec": int(int(p["approxDurationMs"] or 0) / 1000),
            "url": p["_url"],
        })
    streams.sort(key=lambda s: s["bitrate"], reverse=True)
    return {"player_response": {"videoDetails": player_details},
            "perf_urls": perf_urls, "streams": streams}

_CAP_SETUP = (
    "var vid = arguments[0];"
    "var p = document.getElementById('movie_player');"
    "if(!p) return 'no-player';"
    "var v = document.querySelector('video.html5-main-video') || document.querySelector('video');"
    "if(!v) return 'no-video';"
    "try { v.muted = false; } catch(e) {}"
    "var stream = v.captureStream ? v.captureStream() : v.mozCaptureStream();"
    "var tracks = stream.getAudioTracks();"
    "if(!tracks.length) return 'no-audio-track';"
    "var mix = new MediaStream(tracks);"
    "var rec = new MediaRecorder(mix, {mimeType:'audio/webm;codecs=opus'});"
    "window.__capChunks = []; window.__capDone = false; window.__capB64 = '';"
    "rec.ondataavailable = function(e){ if(e.data && e.data.size) window.__capChunks.push(e.data); };"
    "rec.onstop = function(){"
    "  var blob = new Blob(window.__capChunks, {type:'audio/webm'});"
    "  var fr = new FileReader();"
    "  fr.onload = function(){ window.__capB64 = fr.result.split(',')[1]; window.__capDone = true; };"
    "  fr.onerror = function(){ window.__capDone = true; };"
    "  fr.readAsDataURL(blob);"
    "};"
    "window.__capRec = rec;"
    "window.__capVid = vid;"
    "return 'armed|tracks=' + tracks.length + '|recState=' + rec.state;"
)

_CAP_START = (
    "var p = document.getElementById('movie_player');"
    "if(!p) return 'no-player';"
    "try { p.seekTo(0, true); } catch(e) {}"
    "window.__capRec.start(1000);"
    "try { p.playVideo(); } catch(e) {}"
    "return 'recording|state=' + window.__capRec.state;"
)

_CAP_PLAY = (
    "var p = document.getElementById('movie_player');"
    "if(!p) return 'no-player';"
    "try { p.loadVideoById(window.__capVid); } catch(e) {}"
    "try { p.setVolume(100); } catch(e) {}"
    "try { p.playVideo(); } catch(e) {}"
    "return 'play-called';"
)


def capture_audio_via_browser(video_id: str, raw_cookie: str,
                              max_seconds: int = 1200) -> dict:
    """Last-resort realtime capture: plays the song muted in Chrome and
    records the audio track via MediaRecorder. Returns opus/webm bytes.
    Works everywhere but takes ~the duration of the song."""
    from seleniumbase import SB  # lazy import

    data: Optional[bytes] = None
    title = artist = None
    duration = 0

    with SB(uc=True, xvfb=True, locale_code="en", disable_csp=True,
            maximize=True, chromium_arg="--autoplay-policy=no-user-gesture-required") as sb:
        sb.open("https://music.youtube.com/?hl=en")
        sb.sleep(2)
        for c in parse_cookies(raw_cookie):
            domain = (c.get("domain") or ".youtube.com").lstrip(".")
            if "youtube" not in domain:
                continue
            ck = {"name": c["name"], "value": c["value"],
                  "domain": "." + domain.lstrip("."), "path": c.get("path") or "/"}
            try:
                sb.driver.add_cookie(ck)
            except Exception:
                pass
        sb.uc_open_with_reconnect(
            f"https://music.youtube.com/watch?v={video_id}&hl=en",
            reconnect_time=5)
        try:
            sb.reconnect()
        except Exception:
            pass
        sb.sleep(4)

        try:
            det = _js(sb,
                "var p=document.getElementById('movie_player');"
                "if(!(p&&p.getPlayerResponse)) return '{}';"
                "var vd=(p.getPlayerResponse().videoDetails)||{};"
                "return JSON.stringify({title:vd.title,author:vd.author,"
                "lengthSeconds:parseInt(vd.lengthSeconds||'0')});")
            d = json.loads(det or "{}")
            title, artist = d.get("title"), d.get("author")
            duration = int(d.get("lengthSeconds") or 0)
        except Exception:
            pass

        # start playback first so the live video element is the target
        _js(sb, _CAP_PLAY)
        play_deadline = time.time() + 45
        playing = False
        while time.time() < play_deadline:
            try:
                state = _js(sb,
                    "var p=document.getElementById('movie_player');"
                    "return p&&p.getPlayerState?p.getPlayerState():-9;")
                cur = _js(sb,
                    "var v=document.querySelector('video');"
                    "return v?String(v.currentTime):'-1';")
                if state in (1, 3) and float(cur or 0) > 0.5:
                    playing = True
                    break
            except Exception:
                pass
            sb.sleep(2)
            try:
                _js(sb, "var p=document.getElementById('movie_player');"
                        "try{p.playVideo();}catch(e){}")
            except Exception:
                pass
        if not playing:
            raise RuntimeError("capture: playback never started")

        armed = _js(sb, _CAP_SETUP, video_id)
        if not str(armed).startswith("armed"):
            raise RuntimeError(f"capture setup failed: {armed}")
        started = _js(sb, _CAP_START)
        if not str(started).startswith("recording"):
            raise RuntimeError(f"capture start failed: {started}")

        deadline = time.time() + min(max_seconds, (duration or 600) + 90)
        ended = False
        while time.time() < deadline:
            try:
                state = _js(sb,
                    "var p=document.getElementById('movie_player');"
                    "return p&&p.getPlayerState?p.getPlayerState():-9;")
                got = _js(sb,
                    "var b=(window.__capChunks||[]).reduce(function(a,c){return a+c.size},0);"
                    "return String(b);")
                if state == 0:  # ENDED
                    ended = True
                    break
                if not ended and state in (1, 3) or True:
                    pass
            except Exception:
                pass
            sb.sleep(3)

        # stop recorder and wait for blob
        try:
            _js(sb, "if(window.__capRec && window.__capRec.state!=='inactive')"
                    "{window.__capRec.stop();}")
        except Exception:
            pass
        b64_deadline = time.time() + 60
        while time.time() < b64_deadline:
            try:
                done = _js(sb, "return window.__capDone === true;")
                if done:
                    break
            except Exception:
                pass
            sb.sleep(2)
        # pull base64 in slices
        b64 = ""
        while True:
            part = _js(sb,
                f"return (window.__capB64||'').slice({len(b64)}, {len(b64) + 1000000});")
            if not part:
                break
            b64 += part
        if b64:
            import base64 as _b64
            data = _b64.b64decode(b64)

    return {"data": data, "title": title, "artist": artist,
            "durationSec": duration, "container": "weba"}
