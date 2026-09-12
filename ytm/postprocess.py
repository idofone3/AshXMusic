"""
Post-processing: turn every raw download into a tagged .mp4 with embedded
cover art (thumbnail) — yt-dlp `--embed-thumbnail --convert-to mp4` style,
but always `-c copy` when the source is AAC (instant) and a fast AAC
transcode only when the source is opus/vorbis.

Also fetches the best thumbnail for a video without any premade YT lib:
i.ytimg.com static URLs (maxresdefault -> hqdefault -> mqdefault).
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Dict, Optional

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

_THUMB_URLS = [
    "https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
    "https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
    "https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
]

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://www.youtube.com/",
}


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(cmd, timeout: int = 180):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or b"")[-800:].decode("utf-8", "replace")
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: {tail}")
    return p


def probe(path: str) -> Dict:
    """Return {duration, codec, has_cover, streams} via ffprobe."""
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, timeout=30)
        d = json.loads(p.stdout or "{}")
    except Exception:
        return {"duration": 0, "codec": None, "has_cover": False}
    streams = d.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = [s for s in streams if s.get("codec_type") == "video"]
    return {
        "duration": float((d.get("format") or {}).get("duration") or 0),
        "codec": (audio or {}).get("codec_name"),
        "bitrate": int((d.get("format") or {}).get("bit_rate") or 0),
        "has_cover": any(s.get("disposition", {}).get("attached_pic") == 1
                         for s in video),
        "streams": len(streams),
    }


def fetch_thumbnail(video_id: str, fallback_url: Optional[str] = None,
                    out_dir: Optional[str] = None) -> Optional[str]:
    """Download the best thumbnail, normalize to JPEG square-ish cover.
    Returns path or None."""
    import requests

    out_dir = out_dir or tempfile.mkdtemp(prefix="ytmthumb_")
    os.makedirs(out_dir, exist_ok=True)
    candidates = []
    if fallback_url:
        candidates.append(fallback_url)
    candidates += [u.format(vid=video_id) for u in _THUMB_URLS]

    raw = os.path.join(out_dir, f"thumb_{video_id}.bin")
    for url in candidates:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=(10, 20))
            if r.status_code != 200 or len(r.content) < 2000:
                continue
            if "image" not in (r.headers.get("content-type") or "image"):
                continue
            with open(raw, "wb") as f:
                f.write(r.content)
            break
        except Exception:
            continue
    if not os.path.exists(raw):
        return None

    # normalize: JPEG, max 640x640 (small, fast, perfect for cover art)
    jpg = os.path.join(out_dir, f"cover_{video_id}.jpg")
    try:
        _run([FFMPEG, "-y", "-i", raw, "-vf",
              "scale='min(640,iw)':'min(640,ih)':force_original_aspect_ratio=decrease",
              "-frames:v", "1", "-q:v", "3", jpg], timeout=60)
    except Exception:
        return None
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    return jpg if os.path.exists(jpg) and os.path.getsize(jpg) > 500 else None


def to_mp4(src: str, dest: str, title: str = "", artist: str = "",
           thumb: Optional[str] = None, album: str = "YouTube Music") -> Dict:
    """Convert any audio container to a tagged mp4 with embedded cover.

    - AAC source  -> `-c:a copy`  (remux only, ~0.1s for a 4-min song)
    - opus/vorbis -> `-c:a aac`   (fast encode, preserves 48kHz stereo)
    - cover art   -> mjpeg attached_pic stream (shows in players/Telegram)
    Writes to dest atomically (tmp + replace). Returns ffprobe info.
    """
    src = os.path.abspath(src)
    dest = os.path.abspath(dest)
    if not os.path.exists(src):
        raise RuntimeError(f"source missing: {src}")

    info = probe(src)
    codec = (info.get("codec") or "").lower()
    copy_audio = codec in ("aac",)

    tmp_fd, tmp_out = tempfile.mkstemp(prefix="mp4_", suffix=".part",
                                       dir=os.path.dirname(dest) or ".")
    os.close(tmp_fd)
    os.remove(tmp_out)  # ffmpeg wants to create it itself

    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if thumb and os.path.exists(thumb):
        cmd += ["-i", thumb]

    cmd += ["-map", "0:a:0"]
    if thumb and os.path.exists(thumb):
        cmd += ["-map", "1:v:0"]

    if copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        # fast, high-quality transcode (opus -> AAC-LC)
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

    if thumb and os.path.exists(thumb):
        cmd += ["-c:v:0", "mjpeg", "-q:v:0", "3",
                "-disposition:v:0", "attached_pic",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)"]

    cmd += ["-f", "mp4", "-movflags", "+faststart",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album_artist={artist}",
            "-metadata", f"album={album}",
            tmp_out]

    t0 = time.time()
    try:
        _run(cmd, timeout=300)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        os.replace(tmp_out, dest)
    except Exception:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        raise
    out = probe(dest)
    out["convertSec"] = round(time.time() - t0, 2)
    out["audioOp"] = "copy" if copy_audio else "transcode"
    return out


def to_mp3(src: str, dest: str, title: str = "", artist: str = "",
           thumb: Optional[str] = None,
           album: str = "YouTube Music") -> Dict:
    """Transcode any audio source to a full-quality 320 kbps mp3 with ID3v2
    tags and embedded cover art (APIC). Requires ffmpeg/libmp3lame.
    Writes atomically. Returns ffprobe info."""
    src = os.path.abspath(src)
    dest = os.path.abspath(dest)
    if not os.path.exists(src):
        raise RuntimeError(f"source missing: {src}")
    if not available():
        raise RuntimeError("ffmpeg not available for mp3 conversion")

    tmp_fd, tmp_out = tempfile.mkstemp(prefix="mp3_", suffix=".part",
                                       dir=os.path.dirname(dest) or ".")
    os.close(tmp_fd)
    os.remove(tmp_out)  # ffmpeg wants to create it itself

    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    has_thumb = thumb and os.path.exists(thumb)
    if has_thumb:
        cmd += ["-i", thumb]

    cmd += ["-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "320k", "-ar",
            "44100", "-ac", "2"]
    if has_thumb:
        cmd += ["-map", "1:v:0", "-c:v:0", "mjpeg", "-q:v:0", "3",
                "-disposition:v:0", "attached_pic",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)"]

    cmd += ["-id3v2_version", "3", "-write_id3v1", "1",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album_artist={artist}",
            "-metadata", f"album={album}",
            "-f", "mp3", tmp_out]

    t0 = time.time()
    try:
        _run(cmd, timeout=300)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        os.replace(tmp_out, dest)
    except Exception:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        raise
    out = probe(dest)
    out["convertSec"] = round(time.time() - t0, 2)
    out["audioOp"] = "mp3-320k"
    return out
