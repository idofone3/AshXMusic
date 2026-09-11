"""Final test: sabr_download_via_browser for Mann Mera — full song, fast."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ytm import fastdl
from ytm.engine import Engine

BASE = os.path.dirname(os.path.abspath(__file__))
engine = Engine(os.path.join(BASE, "cookies.txt"), os.path.join(BASE, "downloads"))

res = engine.search("mann mera gajendra verma", flt="songs", limit=5)
target = next((r for r in res if "mann mera" in r["title"].lower()), res[0])
print(f"target: {target['title']} — {target['artist']} {target['videoId']}")

t0 = time.time()
r = fastdl.sabr_download_via_browser(
    target["videoId"], engine._cookie_raw,
    progress=lambda n: print(f"   {n/1e6:.1f} MB", flush=True))
dt = time.time() - t0

print(f"itag={r['itag']} mime={r['mimeType']} clen={r['contentLength']} "
      f"complete={r['complete']}")
print(f"downloaded {len(r['data'])/1e6:.2f} MB in {dt:.1f}s "
      f"({len(r['data'])/1e6/max(dt,.01):.2f} MB/s)")

out = os.path.join(BASE, "downloads", "mann_mera_sabr.webm")
open(out, "wb").write(r["data"])
p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                    "format=format_name,duration,size,bit_rate",
                    "-show_entries", "stream=codec_name,sample_rate,channels",
                    "-of", "json", out], capture_output=True, text=True)
print("ffprobe:", p.stdout or p.stderr[:200])
print("TOTAL wall time:", round(dt, 1), "s for a 3:48 song")
