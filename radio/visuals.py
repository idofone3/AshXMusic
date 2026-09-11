"""Builds the 1280x720@30 station frame with ffmpeg.

Layout (YT Music vibe):
  - full-bleed blurred+darkened cover as background
  - square cover art floating centered
  - top-left LIVE dot + station name
  - bottom strip: title / artist, elapsed clock, duration, progress bar
    that fills in 64 segments synced to the audio.
"""
import os
import re

W, H = 1280, 720
BAR_SEG = 64
BAR_Y = H - 32
GREEN = "0x1DB954"
RED = "0xE1283C"

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
_DEVANAGARI = [
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
]


def _has_devanagari(s: str) -> bool:
    return any("\u0900" <= ch <= "\u097F" for ch in s or "")


def pick_font(text: str = "") -> str:
    if _has_devanagari(text):
        for p in _DEVANAGARI:
            if os.path.exists(p):
                return p
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return "DejaVuSans-Bold"


def clean_text(s: str, limit: int = 58) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: limit - 1] + "…" if len(s) > limit else (s or " ")


def _dt(**kw) -> str:
    """drawtext with keyword args (values assumed pre-escaped)."""
    inner = ":".join(f"{k}={v}" for k, v in kw.items())
    return f"drawtext={inner}"


def build_song_graph(title: str, artist: str, duration: float,
                     workdir: str, station: str = "Hindi Hits Radio") -> str:
    """filter_complex for one song. Inputs: 0=audio mp4, 1=looped cover."""
    os.makedirs(workdir, exist_ok=True)
    t_path = os.path.join(workdir, "title.txt")
    a_path = os.path.join(workdir, "artist.txt")
    s_path = os.path.join(workdir, "station.txt")
    d_path = os.path.join(workdir, "dur.txt")
    with open(t_path, "w") as f:
        f.write(clean_text(title))
    with open(a_path, "w") as f:
        f.write(clean_text(artist, 46))
    with open(s_path, "w") as f:
        f.write(station)
    with open(d_path, "w") as f:
        f.write(_fmt_clock(duration))
    ft = pick_font(title)
    fs = pick_font(station)
    elapsed = (r"'%{eif\:trunc(t/60)\:d\:2}"
               r"\:%{eif\:trunc(mod(t\,60))\:d\:2}'")

    f = []
    # blurred background (downscale -> blur -> upscale = fast & smooth)
    f.append(f"[1:v]scale=192:108:force_original_aspect_ratio=increase,"
             f"crop=192:108,boxblur=luma_radius=10:luma_power=2:"
             f"chroma_radius=5,scale={W}:{H},"
             f"eq=brightness=-0.15:saturation=1.30[bg]")
    # cover: center-crop to a true square (kills 4:3 pillarbox bars)
    f.append("[1:v]scale=430:430:force_original_aspect_ratio=increase:"
             "flags=bicubic,crop=430:430[cover]")
    f.append(f"[bg][cover]overlay=(W-w)/2:88:shortest=1[v1]")
    # bottom strip backdrop + top-left live dot
    f.append(f"[v1]drawbox=x=0:y={H-170}:w={W}:h=170:color=black@0.40:t=fill[v2]")
    f.append(f"[v2]drawbox=x=48:y=46:w=14:h=14:color={RED}@0.95:t=fill[v3]")

    texts = [
        _dt(fontfile=fs, textfile=s_path, fontsize=23,
            fontcolor="white@0.92", x="76", y="40"),
        _dt(fontfile=ft, textfile=t_path, fontsize=40,
            fontcolor="white", x="64", y=f"{H-154}"),
        _dt(fontfile=ft, textfile=a_path, fontsize=26,
            fontcolor="0xE6E6E6", x="64", y=f"{H-110}"),
        _dt(fontfile=ft, text=elapsed, fontsize=20,
            fontcolor="white@0.95", x="64", y=f"{H-72}"),
        _dt(fontfile=ft, textfile=d_path, fontsize=20,
            fontcolor="white@0.80", x=f"W-tw-64", y=f"{H-72}"),
    ]
    f.append(f"[v3]" + ",".join(texts) + f"[v4]")

    # progress track + 64 animated segments
    f.append(f"[v4]drawbox=x=64:y={BAR_Y}:w={W-128}:h=8:"
             f"color=white@0.18:t=fill[v5]")
    seg_w = (W - 128) / BAR_SEG
    segs = []
    for i in range(BAR_SEG):
        t0 = duration * i / BAR_SEG
        segs.append(f"drawbox=x={64 + i * seg_w:.1f}:y={BAR_Y}:"
                    f"w={seg_w - 3:.1f}:h=8:color={GREEN}@0.95:t=fill:"
                    f"enable='between(t,{t0:.2f},{duration:.2f})'")
    f.append(f"[v5]" + ",".join(segs) + ",format=yuv420p[vout]")
    return ";".join(f)


def build_slate_graph(workdir: str,
                      station: str = "HINDI HITS RADIO  •  24/7") -> str:
    """20s idle slate: dark bg, station name, hint text. Input: none."""
    os.makedirs(workdir, exist_ok=True)
    s1 = os.path.join(workdir, "slate1.txt")
    s2 = os.path.join(workdir, "slate2.txt")
    with open(s1, "w") as f:
        f.write(station)
    with open(s2, "w") as f:
        f.write("send a song name in chat — it plays next")
    ft = pick_font(station)
    return ";".join([
        f"color=c=0x10131C:s={W}x{H}:r=30[bg]",
        f"[bg]{_dt(fontfile=ft, text='♪', fontsize=150, fontcolor='#1DB954', x='(w-text_w)/2', y='200')}[v1]",
        f"[v1]{_dt(fontfile=ft, textfile=s1, fontsize=52, fontcolor='white', x='(w-text_w)/2', y='380')}"
        f",{_dt(fontfile=ft, textfile=s2, fontsize=30, fontcolor='white@0.75', x='(w-text_w)/2', y='470')}"
        f",format=yuv420p[vout]",
    ])


def _fmt_clock(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def ffmpeg_common_args(ts_path: str, duration: float) -> list:
    return [
        "-r", "30", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-b:v", "2600k", "-maxrate", "2600k", "-bufsize", "1800k",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-t", f"{duration:.2f}",
        "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0",
        "-mpegts_flags", "+resend_headers", ts_path,
    ]
