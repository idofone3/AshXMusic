"""
Pure-python YouTube Music InnerTube client.

Flow:
  1. GET music.youtube.com with the user's cookies -> parse `ytcfg.set({...})`
     for API key / INNERTUBE_CONTEXT / client version / player js url.
  2. POST /youtubei/v1/search  -> parsed music shelf results.
  3. POST /youtubei/v1/player  -> streamingData, tried across several clients
     (tv_embedded / ios / android / web_remix) until one returns playable URLs.
     Ciphered web formats get deciphered via ytm.decipher.
"""
import hashlib
import json
import re
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import requests

from .cookies import parse_cookies
from .decipher import make_decipher

MUSIC_ORIGIN = "https://music.youtube.com"
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SEARCH_FILTERS = {
    "all": None,
    "songs": "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D",
    "videos": "EgWKAQIQAWoKEAkQChAFEAMQBA%3D%3D",
    "albums": "EgWKAQIYAWoKEAkQChAFEAMQBA%3D%3D",
    "artists": "EgWKAQIgAWoKEAkQChAFEAMQBA%3D%3D",
    "playlists": "EgeKAQQoAEABagoQAxAEEAkQBRAK%3D",
}

ITAG_AUDIO = {
    "140": ("m4a", "mp4a.40.2", "~128kbps"),
    "141": ("m4a", "mp4a.40.2", "~256kbps"),
    "256": ("m4a", "mp4a.40.5", "~48kbps"),
    "258": ("m4a", "mp4a.40.5", "~48kbps"),
    "599": ("m4a", "mp4a.40.5", "~30kbps"),
    "139": ("m4a", "mp4a.40.5", "~48kbps"),
    "251": ("weba", "opus", "~160kbps"),
    "250": ("weba", "opus", "~70kbps"),
    "249": ("weba", "opus", "~50kbps"),
}

CLIENT_SPECS: Dict[str, Dict] = {
    "android_vr": {
        "host": "https://youtubei.googleapis.com/youtubei/v1",
        "key": "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
        "ctx": {"client": {"clientName": "ANDROID_VR", "clientVersion": "1.60.19",
                           "deviceMake": "Oculus", "deviceModel": "Quest 3",
                           "osName": "Android", "osVersion": "12L",
                           "androidSdkVersion": 32, "hl": "en", "gl": "US"}},
        "ua": ("com.google.android.apps.youtube.vr.oculus/1.60.19 "
               "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"),
        "headers": {"X-Youtube-Client-Name": "28", "X-Youtube-Client-Version": "1.60.19"},
        "use_cookies": True,
    },
    "ios": {
        "host": "https://www.youtube.com/youtubei/v1",
        "key": None,
        "ctx": {"client": {"clientName": "IOS", "clientVersion": "19.45.4",
                           "deviceMake": "Apple", "deviceModel": "iPhone16,2",
                           "osName": "iPhone", "osVersion": "18.1.0.22B83",
                           "hl": "en", "gl": "US"}},
        "ua": "com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)",
        "headers": {"X-Youtube-Client-Name": "5", "X-Youtube-Client-Version": "19.45.4"},
        "use_cookies": False,
    },
    "android": {
        "host": "https://www.youtube.com/youtubei/v1",
        "key": None,
        "ctx": {"client": {"clientName": "ANDROID", "clientVersion": "19.44.38",
                           "androidSdkVersion": 30, "osName": "Android",
                           "osVersion": "11", "hl": "en", "gl": "US"}},
        "ua": "com.google.android.youtube/19.44.38 (Linux; U; Android 11) gzip",
        "headers": {"X-Youtube-Client-Name": "3", "X-Youtube-Client-Version": "19.44.38"},
        "use_cookies": False,
    },
    "tv": {
        "host": "https://www.youtube.com/youtubei/v1",
        "key": "AIzaSyDCU8hByM-4DrUqRUYnGn-3llEO78bcxq8",
        "ctx": {"client": {"clientName": "TVHTML5", "clientVersion": "7.20250312.16.00",
                           "hl": "en", "gl": "US"}},
        "ua": "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
        "headers": {},
        "use_cookies": False,
    },
    "tv_embedded": {
        "host": "https://www.youtube.com/youtubei/v1",
        "key": "AIzaSyDCU8hByM-4DrUqRUYnGn-3llEO78bcxq8",
        "ctx": {"client": {"clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER",
                           "clientVersion": "2.0", "hl": "en", "gl": "US"},
                "thirdParty": {"embedUrl": "https://www.google.com"}},
        "ua": ("Mozilla/5.0 (PlayStation; PlayStation 4/12.00) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15"),
        "headers": {},
        "use_cookies": False,
    },
    "web_remix": {
        "host": MUSIC_ORIGIN + "/youtubei/v1",
        "key": None,
        "ctx": None,  # real page context from ytcfg
        "ua": UA_DESKTOP,
        "headers": {},
        "use_cookies": True,
    },
}


class YTApiError(Exception):
    pass


def _collect(obj, key: str, out: List):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _collect(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, out)


def _runs(node: Dict) -> str:
    try:
        runs = node["runs"]
    except (KeyError, TypeError):
        try:
            runs = node["text"]["runs"]
        except (KeyError, TypeError):
            return ""
    try:
        return "".join(r.get("text", "") for r in runs)
    except (AttributeError, TypeError):
        return ""


def parse_list_item(r: Dict) -> Optional[Dict]:
    cols = [_runs(c.get("musicResponsiveListItemFlexColumnRenderer",
                        c.get("musicResponsiveListItemFlexCellRenderer", {})))
            for c in r.get("flexColumns", [])]
    if not cols or not cols[0]:
        return None
    vid = None
    for path in (
        lambda x: x["playlistItemData"]["videoId"],
        lambda x: x["overlay"]["musicItemThumbnailOverlayRenderer"]["content"]
                  ["musicPlayButtonRenderer"]["playNavigationEndpoint"]
                  ["watchEndpoint"]["videoId"],
        lambda x: x["navigationEndpoint"]["watchEndpoint"]["videoId"],
        lambda x: (x["flexColumns"][0]["musicResponsiveListItemFlexColumnRenderer"]
                   ["text"]["runs"][0]["navigationEndpoint"]
                   ["watchEndpoint"]["videoId"]),
    ):
        try:
            vid = path(r)
            break
        except (KeyError, TypeError, IndexError):
            continue
    if not vid:
        return None

    duration = None
    for c in cols:
        m = re.fullmatch(r"(\d{1,2}:)?\d{1,2}:\d{2}", c.strip())
        if m:
            duration = m.group(0)
            break

    kind = "song"
    artist = album = None
    extra = cols[1] if len(cols) > 1 else ""
    if " • " in extra:
        parts = [p.strip() for p in extra.split(" • ")]
        if parts[0].lower() in ("song", "video", "single", "ep"):
            kind = parts[0].lower()
            parts = parts[1:]
        if parts:
            artist = parts[0]
    if len(cols) > 2:
        c2 = cols[2]
        if c2 and not re.fullmatch(r"(\d{1,2}:)?\d{1,2}:\d{2}", c2.strip()) \
                and " • " not in c2:
            album = c2
        elif " • " in c2:
            album = c2.split(" • ")[0]

    thumbs = []
    try:
        thumbs = (r["thumbnail"]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"])
    except (KeyError, TypeError):
        pass

    return {
        "videoId": vid,
        "type": kind,
        "title": cols[0],
        "artist": artist,
        "album": album,
        "duration": duration,
        "thumbnail": thumbs[-1]["url"] if thumbs else None,
    }


class Innertube:
    def __init__(self, raw_cookie: str = ""):
        self.raw_cookie = raw_cookie or ""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA_DESKTOP,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Origin": MUSIC_ORIGIN,
            "Referer": MUSIC_ORIGIN + "/",
        })
        self.ytcfg: Optional[Dict] = None
        self._cfg_ts = 0.0
        self._player_js: Optional[str] = None
        self._player_js_ts = 0.0
        self.set_cookie(self.raw_cookie)

    # ---------------- cookies ----------------

    def set_cookie(self, raw: str):
        self.raw_cookie = raw or ""
        self.session.cookies.clear()
        for c in parse_cookies(self.raw_cookie):
            domain = (c.get("domain") or ".youtube.com").lstrip(".")
            if "youtube" not in domain:
                continue
            self.session.cookies.set(c["name"], c["value"],
                                     domain=domain, path=c.get("path") or "/")
        self.ytcfg = None  # force re-fetch with new cookies

    def cookie_summary(self) -> Dict:
        return {"count": len(self.session.cookies)}

    def has_cookies(self) -> bool:
        names = {c.name for c in self.session.cookies}
        return bool(names & {"SID", "__Secure-1PSID", "__Secure-3PSID"})

    # ---------------- ytcfg ----------------

    def load_ytcfg(self, force: bool = False) -> Dict:
        if self.ytcfg and not force and time.time() - self._cfg_ts < 1800:
            return self.ytcfg
        r = self.session.get(MUSIC_ORIGIN + "/?hl=en", timeout=25)
        r.raise_for_status()
        m = re.search(r"ytcfg\.set\(", r.text)
        if not m:
            raise YTApiError("ytcfg not found on music.youtube.com (bot wall / bad cookies?)")
        open_idx = r.text.index("{", m.start())
        try:
            cfg, _ = json.JSONDecoder().raw_decode(r.text[open_idx:])
        except json.JSONDecodeError as e:
            raise YTApiError(f"ytcfg parse failed: {e}")
        self.ytcfg = cfg
        self._cfg_ts = time.time()
        return cfg

    @property
    def api_key(self) -> str:
        return self.load_ytcfg().get("INNERTUBE_API_KEY", "")

    @property
    def page_context(self) -> Dict:
        ctx = self.load_ytcfg().get("INNERTUBE_CONTEXT")
        if not ctx:
            raise YTApiError("INNERTUBE_CONTEXT missing from ytcfg")
        return ctx

    def fetch_player_js(self) -> str:
        if self._player_js and time.time() - self._player_js_ts < 3600:
            return self._player_js
        cfg = self.load_ytcfg()
        url = cfg.get("PLAYER_JS_URL")
        if not url:
            html = self.session.get(MUSIC_ORIGIN + "/?hl=en", timeout=25).text
            m = re.search(r'"jsUrl"\s*:\s*"(/s/player/[^"]+)"', html) \
                or re.search(r'"(/s/player/[A-Za-z0-9_-]+/'
                             r'(?:player_es6|www-player|base)\.?[A-Za-z0-9_]*)"', html)
            if not m:
                raise YTApiError("player js url not found")
            url = m.group(1)
        r = self.session.get(MUSIC_ORIGIN + url, timeout=30)
        r.raise_for_status()
        self._player_js = r.text
        self._player_js_ts = time.time()
        return self._player_js

    def signature_timestamp(self) -> Optional[int]:
        m = re.search(r'signatureTimestamp\s*:\s*(\d+)', self.fetch_player_js())
        return int(m.group(1)) if m else None

    # ---------------- auth ----------------

    def _auth_headers(self) -> Dict[str, str]:
        if not self.has_cookies():
            return {}
        sapisid = None
        for name in ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"):
            v = self.session.cookies.get(name)
            if v:
                sapisid = v
                break
        if not sapisid:
            return {}
        ts = str(int(time.time()))
        h = hashlib.sha1(f"{sapisid} {MUSIC_ORIGIN} {ts}".encode()).hexdigest()
        return {"Authorization": f"SAPISIDHASH {ts}_{h}",
                "X-Origin": MUSIC_ORIGIN, "X-Goog-AuthUser": "0"}

    # ---------------- low-level call ----------------

    def _call(self, endpoint: str, body: Dict, headers: Optional[Dict] = None,
              client_ctx: Optional[Dict] = None, host: Optional[str] = None,
              use_cookies: bool = True, key: Optional[str] = None) -> Dict:
        host = host or (MUSIC_ORIGIN + "/youtubei/v1")
        ctx = client_ctx if client_ctx is not None else self.page_context
        payload = {"context": ctx, **body}
        params = {"prettyPrint": "false"}
        # the key param is client-bound; mismatched keys trigger
        # HTTP 400 PreconditionFailed on several clients
        if key:
            params["key"] = key
        elif use_cookies and self.api_key:
            params["key"] = self.api_key
        hdrs = {"Content-Type": "application/json", **self._auth_headers()}
        if headers:
            hdrs.update(headers)
        if use_cookies:
            r = self.session.post(f"{host}/{endpoint}", params=params,
                                  json=payload, headers=hdrs, timeout=30)
        else:
            r = requests.post(f"{host}/{endpoint}", params=params, json=payload,
                              headers=hdrs, timeout=30)
        if r.status_code != 200:
            raise YTApiError(f"{endpoint} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------------- search ----------------

    def search(self, query: str, flt: str = "songs", limit: int = 30) -> List[Dict]:
        body: Dict = {"query": query}
        param = SEARCH_FILTERS.get(flt)
        if param:
            body["params"] = param
        data = self._call("search", body)
        shelves: List[Dict] = []
        _collect(data, "musicShelfRenderer", shelves)
        items, seen = [], set()
        for sh in shelves:
            for it in sh.get("contents", []):
                track = parse_list_item(it.get("musicResponsiveListItemRenderer", {}))
                if not track or track["videoId"] in seen:
                    continue
                seen.add(track["videoId"])
                items.append(track)
                if len(items) >= limit:
                    return items
        return items

    # ---------------- player ----------------

    def _player_for_client(self, video_id: str, name: str) -> Dict:
        spec = CLIENT_SPECS[name]
        ctx = spec["ctx"]
        body: Dict = {"videoId": video_id, "contentCheckOk": True, "racyCheckOk": True}
        if name == "web_remix":
            body["playbackContext"] = {"contentPlaybackContext": {
                "html5Preference": "HTML5_PREF_WANTS"}}
            sts = self.signature_timestamp()
            if sts:
                body["playbackContext"]["contentPlaybackContext"]["signatureTimestamp"] = sts
        else:
            body["playbackContext"] = {"contentPlaybackContext": {"signatureTimestamp": self.signature_timestamp() or 0}}
        hdrs = {"User-Agent": spec["ua"], **(spec.get("headers") or {})}
        if name == "web_remix":
            cfg = self.load_ytcfg()
            vid = ((cfg.get("INNERTUBE_CONTEXT") or {}).get("client") or {}).get("visitorData")
            if vid:
                hdrs["X-Goog-Visitor-Id"] = vid
            hdrs["X-YouTube-Client-Name"] = "67"
            hdrs["X-YouTube-Client-Version"] = cfg.get("INNERTUBE_CLIENT_VERSION", "")
        return self._call("player", body, headers=hdrs,
                          client_ctx=ctx, host=spec["host"],
                          use_cookies=spec["use_cookies"], key=spec.get("key"))

    def _finalize_streams(self, pr: Dict) -> List[Dict]:
        sd = pr.get("streamingData") or {}
        fmts = list(sd.get("adaptiveFormats") or [])
        need_decipher = any("signatureCipher" in f and "url" not in f for f in fmts)
        dec: Optional[Callable[[str], str]] = None
        if need_decipher:
            dec = make_decipher(self.fetch_player_js())
        streams = []
        for f in fmts:
            mime = f.get("mimeType", "")
            if not mime.startswith("audio"):
                continue
            if "url" in f:
                url = f["url"]
            elif "signatureCipher" in f and dec:
                sc = parse_qs(f["signatureCipher"], keep_blank_values=True)
                url = sc.get("url", [None])[0]
                s = sc.get("s", [None])[0]
                sp = sc.get("sp", ["sig"])[0]
                if not url or not s:
                    continue
                url = f"{url}&{sp}={dec(s)}"
            else:
                continue
            itag = str(f.get("itag", ""))
            container, codec, q = ITAG_AUDIO.get(itag, ("m4a", "", ""))
            if "webm" in mime or "opus" in mime or "vorbis" in mime:
                container = "weba"
            streams.append({
                "itag": itag,
                "container": container,
                "codec": codec,
                "mimeType": mime.split(";")[0],
                "bitrate": f.get("bitrate"),
                "quality": f.get("audioQuality", "").replace("AUDIO_QUALITY_", "") or q,
                "contentLength": int(f.get("contentLength") or 0),
                "durationSec": int(int(f.get("approxDurationMs") or 0) / 1000),
                "url": url,
            })
        streams.sort(key=lambda s: (s["bitrate"] or 0), reverse=True)
        return streams

    def player(self, video_id: str, order: Optional[List[str]] = None
               ) -> Tuple[Dict, str, List[Dict]]:
        errors = []
        for name in (order or ["android_vr", "ios", "android", "tv", "tv_embedded", "web_remix"]):
            try:
                pr = self._player_for_client(video_id, name)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            status = (pr.get("playabilityStatus") or {}).get("status")
            if status == "OK":
                streams = self._finalize_streams(pr)
                if streams:
                    return pr, name, streams
                errors.append(f"{name}: OK but no audio streams")
            else:
                reason = (pr.get("playabilityStatus") or {}).get("reason", "")
                errors.append(f"{name}: {status} {reason}")
        raise YTApiError("all clients failed -> " + " | ".join(errors))

    @staticmethod
    def track_from_player(pr: Dict) -> Dict:
        vd = pr.get("videoDetails") or {}
        thumbs = [t.get("url") for t in (vd.get("thumbnail", {})
                                         .get("thumbnails") or [])]
        return {
            "videoId": vd.get("videoId"),
            "title": vd.get("title"),
            "author": vd.get("author"),
            "lengthSeconds": int(vd.get("lengthSeconds") or 0),
            "viewCount": vd.get("viewCount"),
            "isLive": vd.get("isLiveContent", False),
            "thumbnail": thumbs[-1] if thumbs else None,
        }
