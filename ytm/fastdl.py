"""
Fast-download engine: yt-dlp-style direct URL extraction, but the InnerTube
`player` requests are fired FROM INSIDE the real SeleniumBase Chrome page
so every bot check passes (real TLS, real fingerprint, real cookies,
valid visitor id + SAPISIDHASH auth).

Flow:
  1. A shared, persistent UC Chrome (Xvfb) stays open on music.youtube.com.
  2. For each non-web client context (ANDROID_VR / IOS / ANDROID / TV...)
     we POST /youtubei/v1/player from page JS via fetch() — declaring the
     client in the body exactly like yt-dlp does. Non-web clients return
     classic adaptiveFormats with plain googlevideo URLs (no SABR).
  3. Every candidate URL is probed in-page (Range 0-1023) and survivors
     are handed back.
  4. engine downloads with parallel HTTP Range requests (full speed,
     ~8 MB chunks x N workers) instead of realtime MediaRecorder capture.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

import requests

from .browser import _js, _js_async

# ------------------------------------------------------------------
# client contexts tried in-page (same idea as yt-dlp client hopping)
# ------------------------------------------------------------------
CLIENTS: List[Dict] = [
    {"name": "android_vr", "cookies": True,
     "key": "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
     "headers": {"X-Youtube-Client-Name": "28",
                 "X-Youtube-Client-Version": "1.60.19"},
     "ctx": {"client": {"clientName": "ANDROID_VR", "clientVersion": "1.60.19",
                        "deviceMake": "Oculus", "deviceModel": "Quest 3",
                        "osName": "Android", "osVersion": "12L",
                        "androidSdkVersion": 32, "hl": "en", "gl": "US"}}},
    {"name": "android_music", "cookies": False,
     "key": "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30",
     "headers": {"X-Youtube-Client-Name": "21",
                 "X-Youtube-Client-Version": "7.27.53"},
     "ctx": {"client": {"clientName": "ANDROID_MUSIC", "clientVersion": "7.27.53",
                        "androidSdkVersion": 34, "osName": "Android",
                        "osVersion": "14", "hl": "en", "gl": "US"}}},
    {"name": "ios_music", "cookies": False,
     "key": "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30",
     "headers": {"X-Youtube-Client-Name": "26",
                 "X-Youtube-Client-Version": "8.22.1"},
     "ctx": {"client": {"clientName": "IOS_MUSIC", "clientVersion": "8.22.1",
                        "deviceMake": "Apple", "deviceModel": "iPhone16,2",
                        "osName": "iPhone", "osVersion": "18.1.0.22B83",
                        "hl": "en", "gl": "US"}}},
    {"name": "ios", "cookies": False,
     "key": "AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc",
     "headers": {"X-Youtube-Client-Name": "5",
                 "X-Youtube-Client-Version": "19.45.4"},
     "ctx": {"client": {"clientName": "IOS", "clientVersion": "19.45.4",
                        "deviceMake": "Apple", "deviceModel": "iPhone16,2",
                        "osName": "iPhone", "osVersion": "18.1.0.22B83",
                        "hl": "en", "gl": "US"}}},
    {"name": "android", "cookies": False,
     "key": "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
     "headers": {"X-Youtube-Client-Name": "3",
                 "X-Youtube-Client-Version": "19.44.38"},
     "ctx": {"client": {"clientName": "ANDROID", "clientVersion": "19.44.38",
                        "androidSdkVersion": 30, "osName": "Android",
                        "osVersion": "11", "hl": "en", "gl": "US"}}},
    {"name": "tv", "cookies": False,
     "key": "AIzaSyDCU8hByM-4DrUqRUYnGn-3llEO78bcxq8",
     "headers": {},
     "ctx": {"client": {"clientName": "TVHTML5",
                        "clientVersion": "7.20250312.16.00",
                        "hl": "en", "gl": "US"}}},
    {"name": "mweb", "cookies": True,
     "headers": {"X-Youtube-Client-Name": "2",
                 "X-Youtube-Client-Version": "2.20250311.03.00"},
     "ctx": {"client": {"clientName": "MWEB",
                        "clientVersion": "2.20250311.03.00",
                        "hl": "en", "gl": "US"}}},
]

# ------------------------------------------------------------------
# page JS: multi-client player fetch (runs on music.youtube.com origin)
# ------------------------------------------------------------------
_PLAYER_FETCH_JS = (
    "var CLIENTS = JSON.parse(arguments[0]);"
    "var SIG_TS  = arguments[1];"
    "var VID     = arguments[2];"
    "var done    = arguments[arguments.length - 1];"
    "(function(){"
    "  async function sha1hex(s){"
    "    var b=new TextEncoder().encode(s);"
    "    var h=await crypto.subtle.digest('SHA-1',b);"
    "    var o='';new Uint8Array(h).forEach(function(x){"
    "      o+=('0'+x.toString(16)).slice(-2);});"
    "    return o;"
    "  }"
    "  async function run(){"
    "    var apiKey='', visitor='', auth=null, sts=0;"
    "    try{ apiKey = ytcfg.get('INNERTUBE_API_KEY') || ''; }catch(e){}"
    "    try{ visitor = ytcfg.get('X_GOOG_VISITOR_ID') || ''; }catch(e){}"
    "    try{ sts = parseInt(ytcfg.get('STS')||'0')||0; }catch(e){}"
    "    if(!sts && SIG_TS) sts = SIG_TS;"
    "    var m=document.cookie.match(/(?:^|;\\s*)SAPISID=([^;]+)/);"
    "    if(m){"
    "      var ts=Math.floor(Date.now()/1000);"
    "      auth='SAPISIDHASH '+ts+'_'+(await sha1hex(m[1]+' '+ts+' '+location.origin));"
    "    }"
    "    var out=[];"
    "    for(var i=0;i<CLIENTS.length;i++){"
    "      var spec=CLIENTS[i];"
    "      var rec={name:spec.name,status:'ERR',reason:'',formats:[]};"
    "      try{"
    "        var body={context:spec.ctx,videoId:VID,"
    "                  contentCheckOk:true,racyCheckOk:true,"
    "                  playbackContext:{contentPlaybackContext:{signatureTimestamp:sts}}};"
    "        var headers={'Content-Type':'application/json'};"
    "        if(spec.cookies && auth) headers['Authorization']=auth;"
    "        if(visitor) headers['X-Goog-Visitor-Id']=visitor;"
    "        for(var k in spec.headers) headers[k]=spec.headers[k];"
    "        var base='/youtubei/v1/player?prettyPrint=false';"
    "        var pr=null;"
    "        var keys=[];"
    "        if(spec.key) keys.push(spec.key);"
    "        keys.push(null);"
    "        if(apiKey && apiKey!==spec.key) keys.push(apiKey);"
    "        for(var ki=0;ki<keys.length;ki++){"
    "          var url=base+(keys[ki]?'&key='+keys[ki]:'');"
    "          var resp=await fetch(url,{method:'POST',headers:headers,"
    "              body:JSON.stringify(body),"
    "              credentials:spec.cookies?'include':'omit'});"
    "          if(!resp.ok){"
    "            rec.reason='HTTP '+resp.status;"
    "            continue;"
    "          }"
    "          pr=await resp.json();"
    "          rec.reason='';"
    "          break;"
    "        }"
    "        if(pr){"
    "          rec.status=pr.playabilityStatus?pr.playabilityStatus.status:'NONE';"
    "          rec.reason=rec.reason||(pr.playabilityStatus?(pr.playabilityStatus.reason||''):'');"
    "          var vd=pr.videoDetails||{};"
    "          rec.title=vd.title;rec.author=vd.author;"
    "          rec.lengthSeconds=parseInt(vd.lengthSeconds||'0');"
    "          var sd=pr.streamingData||{};"
    "          var fmts=(sd.adaptiveFormats||[]);"
    "          for(var j=0;j<fmts.length;j++){"
    "            var f=fmts[j];"
    "            if(!f.mimeType || f.mimeType.indexOf('audio')!==0) continue;"
    "            rec.formats.push({itag:String(f.itag),mimeType:f.mimeType,"
    "              bitrate:f.bitrate||0,contentLength:f.contentLength||0,"
    "              approxDurationMs:f.approxDurationMs||0,"
    "              audioQuality:f.audioQuality||'',"
    "              url:f.url||'',signatureCipher:f.signatureCipher||''});"
    "          }"
    "        }"
    "      }catch(e){ rec.reason=String(e); }"
    "      out.push(rec);"
    "    }"
    "    return JSON.stringify(out);"
    "  }"
    "  run().then(function(s){done(s);},function(e){done('JSERR:'+e);});"
    "})();"
)

_PROBE_REUSE = (
    "var urls = arguments[0];"
    "var done = arguments[arguments.length - 1];"
    "Promise.all(urls.map(function(u){"
    "  return fetch(u, {headers:{Range:'bytes=0-1023'}})"
    "    .then(function(r){ return {u:u, s:r.status}; })"
    "    .catch(function(e){ return {u:u, s:0}; });"
    "})).then(function(rs){ done(JSON.stringify(rs)); });"
)

_ITAG_MAP = {
    "140": ("m4a", "mp4a.40.2"), "141": ("m4a", "mp4a.40.2"),
    "139": ("m4a", "mp4a.40.5"), "256": ("m4a", "mp4a.40.5"),
    "258": ("m4a", "mp4a.40.5"), "599": ("m4a", "mp4a.40.5"),
    "251": ("weba", "opus"), "250": ("weba", "opus"), "249": ("weba", "opus"),
}


# ------------------------------------------------------------------
# UMP (SABR) stream parsing
# ------------------------------------------------------------------
SABR_HOOK_JS = (
    "var done = arguments[arguments.length-1];"
    "(function(){"
    "  if(!window.__gHook){"
    "    var of = window.fetch;"
    "    window.fetch = function(input, init){"
    "      try{"
    "        var u = (typeof input === 'string') ? input : ((input && input.url) || '');"
    "        if(u.indexOf('googlevideo.com') !== -1 && window.__gUrls) window.__gUrls.push(u);"
    "      }catch(e){}"
    "      try { return of.apply(this, arguments); }"
    "      catch(e){ return new Promise(function(res,rej){rej(e);}); }"
    "    };"
    "    var ox = XMLHttpRequest.prototype.open;"
    "    XMLHttpRequest.prototype.open = function(m, u){"
    "      try{ if(u && String(u).indexOf('googlevideo.com') !== -1 && window.__gUrls)"
    "             window.__gUrls.push(String(u)); }catch(e){}"
    "      return ox.apply(this, arguments);"
    "    };"
    "    window.__gHook = true;"
    "  }"
    "  done('armed');"
    "})();"
)

SABR_URLS_JS = (
    "var done = arguments[arguments.length-1];"
    "done(JSON.stringify(Array.from(new Set(window.__gUrls||[]))));"
)

SABR_PLAY_JS = (
    "window.__gUrls = [];"          # wipe previous song's captures
    "window.__dlB64 = '';"          # wipe previous fetch relay
    "window.__dlUrl = '';"
    "var p=document.getElementById('movie_player');"
    "if(!p) return 'no-player';"
    "try{p.stopVideo();}catch(e){}"  # hard-stop anything still buffering
    "try{p.loadVideoById(arguments[0]);}catch(e){}"
    "try{p.playVideo();}catch(e){}"
    "return 'play-called';"
)

SABR_PAUSE_JS = (
    "var p=document.getElementById('movie_player');"
    "if(!p) return 'no-player';"
    "try{p.pauseVideo();}catch(e){}"
    "return 'paused';"
)

# read track metadata from the CURRENT player state (call while still
# holding the browser lock, before any other song can load)
SABR_TRACK_JS = (
    "var p=document.getElementById('movie_player');"
    "if(!(p&&p.getPlayerResponse)) return '{}';"
    "var vd=(p.getPlayerResponse().videoDetails)||{};"
    "var th=[];"
    "try{var a=(vd.thumbnail||{}).thumbnails||[];"
    "for(var i=0;i<a.length;i++) th.push(a[i].url);}catch(e){}"
    "return JSON.stringify({title:vd.title,author:vd.author,"
    "lengthSeconds:parseInt(vd.lengthSeconds||'0'),"
    "videoId:vd.videoId,thumbnail:th[th.length-1]||null});"
)

SABR_STATE_JS = (
    "var p=document.getElementById('movie_player');"
    "return p&&p.getPlayerState?p.getPlayerState():-9;"
)

# fetch a url fully in-page; returns b64 via window var (slice-pulled)
SABR_FETCH_JS = (
    "var done = arguments[arguments.length-1];"
    "(async function(){"
    "  try{"
    "    var r = await fetch(window.__dlUrl, {method:'GET'});"
    "    if(!r.ok) return 'HTTP '+r.status;"
    "    var buf = await r.arrayBuffer();"
    "    var u8 = new Uint8Array(buf);"
    "    var s=''; for(var i=0;i<u8.length;i+=32768){"
    "      s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i+32768,u8.length)));"
    "    }"
    "    window.__dlB64 = btoa(s);"
    "    return 'OK bytes='+u8.length;"
    "  }catch(e){ return 'ERR '+e; }"
    "})().then(function(s){done(s);}, function(e){done('ERR '+e);});"
)


def _read_varint(buf: bytes, i: int):
    shift = 0
    val = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
    return val, i


def parse_ump(data: bytes):
    """Split a UMP/SABR byte stream into (part_type, payload) tuples.
    Tolerates truncated tails (server may cut mid-part)."""
    parts = []
    i = 0
    n = len(data)
    while i < n:
        try:
            ptype = data[i]
            i += 1
            plen, j = _read_varint(data, i)
            if j + plen > n:
                parts.append((ptype, data[j:]))
                break
            parts.append((ptype, data[j:j + plen]))
            i = j + plen
        except Exception:
            break
    return parts


def _strip_media_header(payload: bytes) -> bytes:
    """Deprecated: 0x15 media payloads are raw bytes (no header)."""
    return payload


# UMP part types that are control/metadata, never media
_UMP_META_TYPES = {0x14, 0x1C, 0x1E, 0x22, 0x26, 0x2A, 0x2B, 0x2C,
                   0x35, 0x3A, 0x42, 0x46, 0x47}


def _extract_media(data: bytes) -> bytearray:
    """Concatenate media payloads from a UMP response. Media parts are the
    big binary ones (0x15/0xa0/0x86 observed) and carry raw webm/m4a bytes
    with NO per-part header; everything else is control framing."""
    got = bytearray()
    for ptype, payload in parse_ump(data):
        if ptype in _UMP_META_TYPES or len(payload) < 64:
            continue
        got.extend(payload)
    return got


def _part_hist(data: bytes) -> str:
    hist = {}
    for pt, pl in parse_ump(data):
        key = hex(pt)
        hist[key] = (hist.get(key, 0) or 0) + len(pl)
    return json.dumps(hist)


def _sabr_query_edit(url: str, new_range: str, new_rn: int) -> str:
    from urllib.parse import urlparse, parse_qs, urlencode
    pr = parse_qs(urlparse(url).query, keep_blank_values=True)
    pr["range"] = [new_range]
    pr["rn"] = [str(new_rn)]
    pr["rbuf"] = ["0"]
    return urlparse(url)._replace(query=urlencode(pr, doseq=True)).geturl()


def _in_page_fetch(sb, url: str) -> bytes:
    """Fetch a url from page context (browser TLS fingerprint), return bytes."""
    _js(sb, f"window.__dlUrl = {json.dumps(url)};")
    res = _js_async_long(sb, SABR_FETCH_JS)
    s = str(res)
    if not s.startswith("OK"):
        raise RuntimeError(f"in-page fetch failed: {s[:120]}")
    total = int(s.split("bytes=")[1].split()[0])
    b64 = ""
    while len(b64) < (total * 4 // 3) + 8:
        part = _js(sb, f"return (window.__dlB64||'').slice({len(b64)}, "
                       f"{len(b64) + 3000000});")
        if not part:
            break
        b64 += part
    import base64 as _b64
    data = _b64.b64decode(b64)
    if len(data) != total:
        raise RuntimeError(f"relay mismatch: got {len(data)} of {total}")
    return data


def _gv_video_id(url: str) -> str:
    """Extract the source video id from a googlevideo url.
    The `id` param is either the plain 11-char id or its hex encoding."""
    from urllib.parse import urlparse, parse_qs
    try:
        vid = (parse_qs(urlparse(url).query).get("id") or [""])[0]
    except Exception:
        return ""
    if len(vid) == 11 and vid.isascii():
        return vid
    if vid and len(vid) % 2 == 0:
        try:
            dec = bytes.fromhex(vid).decode("ascii", "ignore")
            if len(dec) == 11:
                return dec
        except Exception:
            pass
    return ""


def _sabr_pick_media_url(urls: List[str],
                         prefer_itags=("140", "141", "256", "258", "599",
                                       "251", "250", "249"),
                         video_id: Optional[str] = None,
                         expect_dur: Optional[float] = None) -> Optional[str]:
    """Pick an audio-only media url from captured player requests.

    SABR googlevideo urls carry an OPAQUE `id` (stream handle, not the
    video id), so id-matching alone cannot block stale urls from a
    previous song. The reliable fingerprint is the `dur` param (content
    duration) matched against the player's current video duration, plus
    the video-id check when a plain id is present."""
    from urllib.parse import urlparse, parse_qs
    cands = []
    for u in urls:
        try:
            q = parse_qs(urlparse(u).query)
        except Exception:
            continue
        if "videoplayback" not in u or "clen" not in q or "range" not in q:
            continue
        mime = (q.get("mime") or [""])[0]
        if not mime.startswith("audio"):
            continue
        itag = (q.get("itag") or ["?"])[0]
        clen = int((q.get("clen") or ["0"])[0])
        try:
            dur = float((q.get("dur") or ["0"])[0])
        except ValueError:
            dur = 0.0
        cands.append({"prio": prefer_itags.index(itag) if itag in prefer_itags
                      else 9, "clen": clen, "dur": dur, "url": u})
    if video_id:
        matched = [c for c in cands if _gv_video_id(c["url"]) == video_id]
        if matched:
            cands = matched
    if expect_dur:
        strict = [c for c in cands
                  if c["dur"] and abs(c["dur"] - expect_dur) <= 2.5]
        if strict:
            cands = strict
        else:
            return None  # everything so far belongs to another song
    if not cands:
        return None
    cands.sort(key=lambda c: (c["prio"], -c["clen"]))
    return cands[0]["url"]


def _looks_like_dead_browser(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in (
        "connection refused", "max retries exceeded", "no such window",
        "browser has crashed", "target window already closed",
        "invalid session id", "chrome not reachable", "cannot find chrome"))


def sabr_download_via_browser(video_id: str, raw_cookie: str,
                              max_wait: int = 20, chunk_bytes: int = 2 << 20,
                              progress: Optional[Callable[[int], None]] = None
                              ) -> dict:
    """Wrapper: some browser sessions get a bad stream badge (server only
    answers control parts) and a dead driver looks the same. Both are
    fixed by rebuilding the shared Chrome once — fresh attestation, fresh
    PO token — then retrying the whole tier."""
    last = None
    for attempt in range(2):
        try:
            return _sabr_once(video_id, raw_cookie, max_wait, chunk_bytes,
                              progress)
        except Exception as e:
            last = e
            retryable = (_looks_like_dead_browser(e)
                         or "refresh rounds" in str(e)
                         or "no media url captured" in str(e))
            if attempt == 0 and retryable:
                try:
                    _SHARED.invalidate()   # brand-new Chrome next acquire
                except Exception:
                    pass
                continue
            raise
    raise last


def sabr_playback_url(video_id: str, raw_cookie: str,
                      max_wait: int = 18) -> dict:
    """Streaming companion to _sabr_once: play the song in the shared
    browser just long enough to capture its pot-carrying videoplayback
    url, pause, and hand the url back — a browser <audio> tag can stream
    that url directly (googlevideo honours HTTP Range, so seeking works).
    No bytes are pulled server-side."""
    from urllib.parse import urlparse, parse_qs, urlencode

    sb = _SHARED.acquire(raw_cookie)
    with _SHARED._lock:
        _js_async_long(sb, SABR_HOOK_JS)
        _js(sb, SABR_PLAY_JS, video_id)
        deadline = time.time() + max_wait
        media_url, track = None, None
        playing_ok, expect_dur, last_state = False, None, None
        t_start, poll = time.time(), 0
        while time.time() < deadline:
            if track is None:
                try:
                    d = json.loads(_js(sb, SABR_TRACK_JS) or "{}")
                    if d.get("title") and (not video_id or
                                           d.get("videoId") == video_id):
                        track = d
                        expect_dur = float(d.get("lengthSeconds") or 0) or None
                except Exception:
                    pass
            try:
                urls = json.loads(_js_async_long(sb, SABR_URLS_JS) or "[]")
            except Exception:
                urls = []
            if not playing_ok:
                try:
                    last_state = _js(sb, SABR_STATE_JS)
                    playing_ok = (last_state == 1)
                except Exception:
                    pass
                if not playing_ok and time.time() - t_start > 15:
                    playing_ok = True  # don't brick on stubborn players
            if playing_ok and expect_dur:
                media_url = _sabr_pick_media_url(urls, video_id=video_id,
                                                 expect_dur=expect_dur)
            if media_url:
                break
            poll += 1
            sb.sleep(0.4 if poll < 25 else 1.0)
            try:
                _js(sb, "var p=document.getElementById('movie_player');"
                        "try{p.playVideo();}catch(e){}")
            except Exception:
                pass
        try:
            _js(sb, SABR_PAUSE_JS)
        except Exception:
            pass
        if not media_url:
            raise RuntimeError(f"no media url captured (state={last_state})")
        # strip per-request framing params -> seekable base stream url
        q = parse_qs(urlparse(media_url).query)
        keep = {k: v[0] for k, v in q.items() if k not in ("range", "rn", "rbuf")}
        url = urlparse(media_url)._replace(query=urlencode(keep)).geturl()
        return {"url": url, "track": track or {},
                "mime": (parse_qs(urlparse(media_url).query)
                         .get("mime") or ["audio/mp4"])[0]}


def _sabr_once(video_id: str, raw_cookie: str,
               max_wait: int, chunk_bytes: int,
               progress: Optional[Callable[[int], None]]) -> dict:
    """yt-dlp-grade download for bot-walled IPs:
    play the song for a few seconds in the shared browser (its own SABR
    requests carry a valid PO token), pause, then request the full byte
    range ourselves in-page and strip the UMP framing. Full song in
    seconds, not realtime.

    When the stream context goes stale mid-download (server answers with
    control parts only — the 0x2b 'reload' signal), we force the player
    to re-buffer (fresh PO token / params), re-capture the media url and
    retry, instead of collapsing to the slow realtime tier."""
    from urllib.parse import urlparse, parse_qs

    sb = _SHARED.acquire(raw_cookie)
    with _SHARED._lock:
        _js_async_long(sb, SABR_HOOK_JS)
        _js(sb, SABR_PLAY_JS, video_id)

        deadline = time.time() + max_wait
        media_url = None
        last_state = None
        poll = 0
        track = None
        expect_dur = None
        playing_ok = False
        t_start = time.time()
        while time.time() < deadline:
            # metadata + duration come from the CURRENT player response;
            # both are read in-lock so no other song can slip in between
            if track is None:
                try:
                    d = json.loads(_js(sb, SABR_TRACK_JS) or "{}")
                    if d.get("title") and (not video_id or
                                           d.get("videoId") == video_id):
                        track = d
                        expect_dur = float(d.get("lengthSeconds") or 0) or None
                except Exception:
                    pass
            try:
                urls = json.loads(_js_async_long(sb, SABR_URLS_JS) or "[]")
            except Exception:
                urls = []
            # gate 1: the player must be actively streaming (state=1) —
            # urls captured before real playback carry a half-issued pot
            # and the server answers them with control parts only
            if not playing_ok:
                try:
                    last_state = _js(sb, SABR_STATE_JS)
                    playing_ok = (last_state == 1)
                except Exception:
                    pass
                if not playing_ok and time.time() - t_start > 15:
                    playing_ok = True  # don't brick on stubborn players
            # gate 2: only pick once the player confirmed the requested
            # video (expect_dur known) — otherwise the first poll could
            # grab an in-flight url from the PREVIOUS song
            if playing_ok and expect_dur:
                media_url = _sabr_pick_media_url(urls, video_id=video_id,
                                                 expect_dur=expect_dur)
            if media_url:
                break
            try:
                last_state = _js(sb, SABR_STATE_JS)
            except Exception:
                pass
            poll += 1
            sb.sleep(0.4 if poll < 25 else 1.0)  # fast poll = fast start
            try:
                _js(sb, "var p=document.getElementById('movie_player');"
                        "try{p.playVideo();}catch(e){}")
            except Exception:
                pass
        if not media_url:
            raise RuntimeError(f"no media url captured (state={last_state})")
        try:
            _js(sb, SABR_PAUSE_JS)
        except Exception:
            pass

        media = bytearray()
        errors = []
        rn_counter = [0]

        def _rn(base: int) -> int:
            rn_counter[0] += 1
            return base + rn_counter[0]

        for rnd in range(4):
            round_start = len(media)
            q = parse_qs(urlparse(media_url).query)
            clen = int((q.get("clen") or ["0"])[0])
            itag = (q.get("itag") or ["?"])[0]
            mime = (q.get("mime") or [""])[0]
            if not clen:
                raise RuntimeError("clen missing from media url")
            rn0 = int((q.get("rn") or ["0"])[0])

            # mode 1: single full-range request (fastest, ~0.6s/4MB)
            got_full = False
            for attempt in range(2):
                try:
                    url = _sabr_query_edit(media_url, f"0-{clen - 1}",
                                           _rn(rn0))
                    data = _in_page_fetch(sb, url)
                    got = _extract_media(data)
                    if len(got) >= clen:
                        media = bytearray(got[:clen])
                        if progress:
                            progress(clen)
                        got_full = True
                        break
                    # keep the best partial (e.g. 5.19/5.19MB): the
                    # windowed tail-fill below can finish it off
                    if len(got) > len(media):
                        media = bytearray(got)
                        errors.append(f"r{rnd} part: {len(got)}/{clen} "
                                      f"parts={_part_hist(data)[:80]}")
                    else:
                        errors.append(f"r{rnd} full: {len(got)}/{clen} "
                                      f"parts={_part_hist(data)[:80]}")
                except Exception as e:
                    errors.append(f"r{rnd} full: {str(e)[:120]}")
                sb.sleep(1.0)
            if got_full:
                break

            if len(media) > round_start:
                sb.sleep(0.5)   # partial media still flowing -> same
                continue        # session may complete on the next round
            # stream context stale (no growth) -> hard-reload the video
            # (new playback session, new cpn/pot), retry with fresh url
            try:
                _js(sb, SABR_PLAY_JS, video_id)
            except Exception:
                pass
            # wait for real playback again before picking (same pot rule)
            sb.sleep(1.0)
            for _ in range(16):
                try:
                    if _js(sb, SABR_STATE_JS) == 1:
                        break
                except Exception:
                    pass
                sb.sleep(0.5)
                try:
                    _js(sb, "var p=document.getElementById('movie_player');"
                            "try{p.playVideo();}catch(e){}")
                except Exception:
                    pass
            fresh = None
            try:
                urls = json.loads(_js_async_long(sb, SABR_URLS_JS) or "[]")
                fresh = _sabr_pick_media_url(urls, video_id=video_id,
                                             expect_dur=expect_dur)
            except Exception:
                pass
            if fresh:
                media_url = fresh
            try:
                _js(sb, SABR_PAUSE_JS)
            except Exception:
                pass
        if not media:
            raise RuntimeError("sabr: no media after refresh rounds | "
                               + "; ".join(errors[-3:]))

        q = parse_qs(urlparse(media_url).query)
        clen = int((q.get("clen") or ["0"])[0])
        itag = (q.get("itag") or ["?"])[0]
        mime = (q.get("mime") or [""])[0]
        rn0 = int((q.get("rn") or ["0"])[0])

        # mode 2: windowed tail-fill — finishes a partial full-range
        # fetch (keeps the MBs already received) or downloads from scratch
        if len(media) < clen:
            pos = len(media)
            stall = 0
            while pos < clen:
                end = min(pos + chunk_bytes, clen) - 1
                start = pos
                # tiny final gap: the server often refuses a ~300-byte
                # range — request a normal window that still ends at the
                # last byte, then cut away the overlap
                overlap = (clen - pos) <= 65536
                if overlap:
                    start = max(0, pos - 65536)
                url = _sabr_query_edit(media_url, f"{start}-{end}",
                                       _rn(rn0))
                try:
                    data = _in_page_fetch(sb, url)
                except Exception as e:
                    stall += 1
                    if stall >= 4:
                        raise RuntimeError(
                            f"sabr fetch failed at {pos}: {e} | {'; '.join(errors[-2:])}")
                    sb.sleep(2)
                    continue
                got = _extract_media(data)
                if not got:
                    stall += 1
                    if stall >= 4:
                        raise RuntimeError(
                            f"sabr returned no media at pos {pos} | "
                            f"{'; '.join(errors[-2:])}")
                    sb.sleep(1)
                    continue
                stall = 0
                if overlap and len(got) >= end - start + 1:
                    # window fully served -> its slice from pos is our tail
                    need = clen - pos
                    media.extend(got[pos - start:pos - start + need])
                    pos = clen
                    if progress:
                        progress(pos)
                    break
                want = min(end - pos + 1, clen - pos)  # never accept more
                take = min(len(got), want)             # than the window ->
                media.extend(got[:take])               # no duplicate tail
                pos += take
                if progress:
                    progress(pos)

        # accept streams clipped by <=4KB (container metadata is intact;
        # the missing tail is <50ms of audio) — duration sanity runs in
        # the engine's _finalize_mp4, so contamination is still caught
        if len(media) > clen:
            media = media[:clen]
        complete = len(media) >= clen - 4096
        if not complete:
            raise RuntimeError(
                f"sabr size mismatch: {len(media)} != {clen} | "
                f"{'; '.join(errors[-2:])}")

    container = "weba" if "webm" in mime or "opus" in mime else "m4a"
    return {"data": bytes(media), "itag": itag, "mimeType": mime.split(";")[0],
            "container": container, "contentLength": clen,
            "track": track, "complete": True,
            "clippedBytes": clen - len(media)}


# ------------------------------------------------------------------
# shared persistent browser (one Chrome for the whole API lifetime)
# ------------------------------------------------------------------
class SharedBrowser:
    """Keeps one UC Chrome alive; injects cookies; restarts on death."""

    def __init__(self):
        self._sb = None
        self._lock = threading.RLock()
        self._cookie_raw = ""
        self._cookie_fp = ""
        self._started_at = 0.0

    def _fingerprint(self, raw_cookie: str) -> str:
        import hashlib
        return hashlib.md5((raw_cookie or "").encode()).hexdigest()

    def _start(self, raw_cookie: str):
        try:
            from .vdisplay import ensure as _ensure_display
            _ensure_display()  # headed chrome on a real X server (trusted pot)
        except Exception:
            pass
        from seleniumbase import SB
        from .browser import _chrome_flags
        from .cookies import parse_cookies
        sb_cm = SB(uc=True, xvfb=True, locale_code="en", disable_csp=True,
                   maximize=True,
                   chromium_arg=_chrome_flags() + ",--mute-audio")
        sb = sb_cm.__enter__()
        sb.open("https://music.youtube.com/?hl=en")
        sb.sleep(2)
        for c in parse_cookies(raw_cookie):
            domain = (c.get("domain") or ".youtube.com").lstrip(".")
            if "youtube" not in domain:
                continue
            ck = {"name": c["name"], "value": c["value"],
                  "domain": "." + domain.lstrip("."),
                  "path": c.get("path") or "/"}
            try:
                sb.driver.add_cookie(ck)
            except Exception:
                pass
        sb.open("https://music.youtube.com/?hl=en")
        sb.sleep(2)
        self._sb = sb
        self._sb_cm = sb_cm
        self._started_at = time.time()

    def acquire(self, raw_cookie: str):
        """Return the live sb object (locked). Restart if dead/cookie changed."""
        with self._lock:
            fp = self._fingerprint(raw_cookie)
            if self._sb is not None and fp != self._cookie_fp:
                self._kill()
            if self._sb is None:
                self._start(raw_cookie)
                self._cookie_fp = fp
            elif (time.time() - self._started_at) > 3600 * 6:
                # recycle long-lived browsers to dodge memory leaks
                try:
                    alive = _js(self._sb, "return 1;")
                except Exception:
                    self._kill()
                    self._start(raw_cookie)
                    self._cookie_fp = fp
            return self._sb

    def _kill(self):
        try:
            self._sb_cm.__exit__(None, None, None)
        except Exception:
            pass
        self._sb = None

    def invalidate(self):
        with self._lock:
            self._kill()
            self._cookie_fp = ""


_SHARED = SharedBrowser()


def set_browser_cookie(raw_cookie: str):
    """Force browser restart with fresh cookies on next use."""
    _SHARED.invalidate()
    _SHARED._cookie_raw = raw_cookie


# ------------------------------------------------------------------
# multi-client player fetch inside the browser
# ------------------------------------------------------------------
def _js_async_long(sb, script: str, *args):
    """_js_async with a bigger timeout for the multi-client loop."""
    try:
        sb.driver.set_script_timeout(150)
    except Exception:
        pass
    return _js_async(sb, script, *args)


def get_direct_streams(video_id: str, raw_cookie: str,
                       sig_ts: int = 0, wait_seconds: int = 60) -> dict:
    sb = _SHARED.acquire(raw_cookie)
    with _SHARED._lock:
        try:
            raw = _js_async_long(sb, _PLAYER_FETCH_JS,
                                 json.dumps(CLIENTS), str(sig_ts or 0), video_id)
        except Exception:
            # browser died mid-call -> hard restart once
            _SHARED.invalidate()
            sb = _SHARED.acquire(raw_cookie)
            raw = _js_async_long(sb, _PLAYER_FETCH_JS,
                                 json.dumps(CLIENTS), str(sig_ts or 0), video_id)
        if isinstance(raw, str) and raw.startswith("JSERR:"):
            raise RuntimeError(raw)

        reports = json.loads(raw or "[]")

        # candidates from every client that returned plain urls
        pending: List[dict] = []
        best_report = None
        for rep in reports:
            if best_report is None or len(rep.get("formats") or []) > \
                    len(best_report.get("formats") or []):
                best_report = rep
            for f in rep.get("formats") or []:
                if f.get("url") and f.get("mimeType", "").startswith("audio"):
                    pending.append({**f, "_client": rep["name"]})

        # dedupe by itag (keep first = highest-priority client)
        seen_itags = set()
        uniq = []
        for p in pending:
            if p["itag"] in seen_itags:
                continue
            seen_itags.add(p["itag"])
            uniq.append(p)

        # probe survivors inside the same browser (googlevideo is IP-locked)
        statuses: Dict[str, int] = {}
        if uniq:
            try:
                rawp = _js_async(sb, _PROBE_REUSE,
                                 [p["url"] for p in uniq])
                statuses = {r["u"]: r["s"]
                            for r in json.loads(rawp or "[]")}
            except Exception:
                statuses = {}

    streams = []
    for p in uniq:
        if statuses.get(p["url"]) not in (200, 206):
            continue
        itag = p["itag"]
        container, codec = _ITAG_MAP.get(itag, ("m4a", ""))
        mime = p["mimeType"]
        if "webm" in mime or "opus" in mime or "vorbis" in mime:
            container, codec = "weba", "opus"
        streams.append({
            "itag": itag,
            "container": container,
            "codec": codec,
            "mimeType": mime.split(";")[0],
            "bitrate": int(p.get("bitrate") or 0),
            "quality": (p.get("audioQuality") or "").replace("AUDIO_QUALITY_", ""),
            "contentLength": int(p.get("contentLength") or 0),
            "durationSec": int(int(p.get("approxDurationMs") or 0) / 1000),
            "url": p["url"],
            "client": p["_client"],
        })
    streams.sort(key=lambda s: s["bitrate"], reverse=True)

    details = {}
    if best_report:
        details = {"videoId": video_id,
                   "title": best_report.get("title"),
                   "author": best_report.get("author"),
                   "lengthSeconds": best_report.get("lengthSeconds") or 0}
    return {"player_response": {"videoDetails": details},
            "reports": [{k: v for k, v in r.items() if k != "formats"}
                        for r in reports],
            "client": (streams[0]["client"] if streams else None),
            "streams": streams}


# ------------------------------------------------------------------
# parallel range downloader (the actual yt-dlp speed part)
# ------------------------------------------------------------------
_DL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://music.youtube.com/",
    "Origin": "https://music.youtube.com",
}


def probe_content_length(url: str) -> int:
    try:
        r = requests.get(url, headers={**_DL_HEADERS, "Range": "bytes=0-0"},
                         timeout=(15, 30))
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            return int(cr.rsplit("/", 1)[-1])
    except Exception:
        pass
    return 0


def parallel_download(url: str, dest: str, total: int = 0,
                      progress: Optional[Callable[[int], None]] = None,
                      workers: int = 8, part_bytes: int = 8 << 20):
    """Download a googlevideo URL at full speed using N parallel Range
    requests of `part_bytes` each, written at the right file offset."""
    if not total:
        total = probe_content_length(url)
    if not total:
        raise RuntimeError("content length unknown, cannot parallel download")

    ranges = [(off, min(off + part_bytes, total) - 1)
              for off in range(0, total, part_bytes)]

    fd = os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, total)
    except OSError:
        pass

    done_bytes = [0]
    lk = threading.Lock()
    first_err = [None]

    def work(rng):
        a, b = rng
        want = b - a + 1
        for attempt in range(4):
            try:
                r = requests.get(url, stream=True, timeout=(20, 90),
                                 headers={**_DL_HEADERS,
                                          "Range": f"bytes={a}-{b}"})
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code} for range {a}-{b}")
                buf = bytearray()
                for chunk in r.iter_content(1 << 18):
                    if chunk:
                        buf.extend(chunk)
                r.close()
                if len(buf) != want:
                    raise RuntimeError(f"short read {len(buf)}/{want}")
                os.pwrite(fd, bytes(buf), a)
                with lk:
                    done_bytes[0] += want
                    if progress:
                        progress(done_bytes[0])
                return
            except Exception as e:
                if attempt == 3:
                    with lk:
                        if first_err[0] is None:
                            first_err[0] = e
                    return
                time.sleep(1.5 * (attempt + 1))

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, ranges))
        if first_err[0]:
            raise RuntimeError(f"parallel download failed: {first_err[0]}")
        if done_bytes[0] != total:
            raise RuntimeError(f"incomplete: {done_bytes[0]}/{total}")
    finally:
        os.close(fd)


# ------------------------------------------------------------------
# instant streaming: cached pot urls + windowed range pump
# ------------------------------------------------------------------
_POT: Dict[str, Dict] = {}
_POT_WAIT: Dict[str, threading.Event] = {}
_POT_LOCK = threading.Lock()
POT_TTL = 900.0            # paused sabr sessions stay answerable ~15 min


def pot_url(video_id: str, raw_cookie: str, max_wait: int = 18) -> dict:
    """Cached, single-flight sabr_playback_url. Concurrent callers wait on
    the in-flight mint instead of double-playing in the browser (which
    would poison both captures with the other song's urls)."""
    for _ in range(3):
        with _POT_LOCK:
            m = _POT.get(video_id)
            if m and time.time() - m["ts"] < POT_TTL:
                return m
            ev = _POT_WAIT.get(video_id)
            mine = False
            if ev is None:
                ev = threading.Event()
                _POT_WAIT[video_id] = ev
                mine = True
        if not mine:
            ev.wait(timeout=60)
            continue
        try:
            m = sabr_playback_url(video_id, raw_cookie, max_wait=max_wait)
            m["ts"] = time.time()
            with _POT_LOCK:
                _POT[video_id] = m
            return m
        finally:
            with _POT_LOCK:
                _POT_WAIT.pop(video_id, None)
            ev.set()
    raise RuntimeError("pot url mint kept failing")


def pot_invalidate(video_id: str):
    with _POT_LOCK:
        _POT.pop(video_id, None)


def sabr_stream(video_id: str, raw_cookie: str, start: int = 0,
                end: Optional[int] = None,
                progress: Optional[Callable[[int], None]] = None):
    """Generator of CLEAN audio bytes (UMP framing stripped) for the byte
    range [start..end] of the song, via the pot-carrying SABR url.

    - Windows are fetched IN-PAGE (real Chrome TLS fingerprint + valid
      trusted PO token -> googlevideo serves datacenter IPs at full
      speed, no throttle, no bot wall).
    - The shared-browser lock is taken PER WINDOW only (~0.5s), so
      downloads and other mints interleave between windows.
    - Stale stream context (server answers control parts only) is healed
      by re-playing the song once (fresh pot/cpn) and resuming at the
      exact byte offset already delivered."""
    from urllib.parse import urlparse, parse_qs
    pos = max(0, int(start))
    first = True
    for attempt in range(2):
        m = pot_url(video_id, raw_cookie)
        url = m["url"]
        q = parse_qs(urlparse(url).query)
        clen = int((q.get("clen") or ["0"])[0])
        if not clen:
            pot_invalidate(video_id)
            continue
        end = clen - 1 if end is None else min(int(end), clen - 1)
        if pos > end:
            return
        sb = _SHARED.acquire(raw_cookie)
        rn = int((q.get("rn") or ["0"])[0])
        stalls = 0
        dead = False
        while pos <= end:
            window = min(1 << 19 if first else 4 << 20, end - pos + 1)
            wstart, wend = pos, pos + window - 1
            try:
                with _SHARED._lock:
                    furl = _sabr_query_edit(url, f"{wstart}-{wend}", rn + 1)
                    rn += 1
                    data = _in_page_fetch(sb, furl)
                got = _extract_media(data)
            except Exception as e:
                stalls += 1
                if _looks_like_dead_browser(e):
                    dead = True
                    break
                if stalls >= 4:
                    break
                time.sleep(1.2)
                continue
            if not got:
                stalls += 1
                if stalls >= 4:
                    break
                time.sleep(1.0)
                continue
            stalls = 0
            want = wend - wstart + 1
            take = bytes(got[:want])
            yield take
            pos += len(take)
            first = False
            if progress:
                progress(pos)
        if pos > end:
            return
        # stale/dead -> fresh playback session, resume where we stopped
        pot_invalidate(video_id)
        if dead:
            try:
                _SHARED.invalidate()   # brand-new Chrome next acquire
            except Exception:
                pass
        if attempt == 0:
            start = pos
            continue
    return
