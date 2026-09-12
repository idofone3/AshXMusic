"""Quick visual test of the new AshXMusic visuals: slate, pause slate,
song graph (with progress tracker at two timestamps + request line)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "radio"))

from radio import visuals as V  # noqa: E402

WORK = "/tmp/axm_work"
os.makedirs(WORK, exist_ok=True)
OUT = "/tmp/axm_vis"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise SystemExit(1)


# 1) slate
graph = V.build_slate_graph(WORK)
run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
     "-f", "lavfi", "-i", f"color=c=0x10131C:s={V.W}x{V.H}:r=30",
     "-filter_complex", graph, "-map", "[vout]",
     "-frames:v", "1", f"{OUT}_slate.png"])
print("slate ok")

# 2) pause slate
graph = V.build_pause_graph(WORK)
run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
     "-f", "lavfi", "-i", "color=c=0x0B0E16:s=1280x720:r=30",
     "-filter_complex", graph, "-map", "[vout]",
     "-frames:v", "1", f"{OUT}_pause.png"])
print("pause slate ok")

# 3) song render: 8s CBR zerolatency, frame at 2s and 6s (tracker moves)
src = None
DL = "/home/z/my-project/ytm-api/downloads"
for n in os.listdir(DL):
    if n.endswith("[NJAv_7lHUIU].mp4"):
        src = os.path.join(DL, n)
        break
if not src:
    print("no cached song, skipping song render test")
    raise SystemExit(0)
cov = f"{OUT}_cover.jpg"
run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
     "-frames:v", "1", cov])
graph = V.build_song_graph("Kesariya (From Brahmastra)", "Arijit Singh",
                           246.0, WORK, station="AshXMusic",
                           queued_by="/play kesariya")
run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
     "-i", src, "-loop", "1", "-i", cov,
     "-filter_complex", graph, "-map", "[vout]", "-map", "0:a"] +
    V.ffmpeg_common_args(f"{OUT}_song.ts", 8.0, volume=110.0))
for t in (2, 6):
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t),
         "-i", f"{OUT}_song.ts", "-frames:v", "1", f"{OUT}_song_t{t}.png"])
print("song render ok")
r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_name,bit_rate", "-of", "csv",
                    f"{OUT}_song.ts"], capture_output=True, text=True)
print(r.stdout)
