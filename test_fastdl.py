"""Test the yt-dlp-style fast path: in-browser multi-client player fetch
-> plain googlevideo urls -> parallel range download. Target: Mann Mera."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ytm import fastdl
from ytm.cookies import parse_cookies
from ytm.engine import Engine

BASE = os.path.dirname(os.path.abspath(__file__))
engine = Engine(os.path.join(BASE, "cookies.txt"), os.path.join(BASE, "downloads"))

# ---- 1. find the song ----
res = engine.search("mann mera gajendra verma", flt="songs", limit=10)
if not res:
    print("search failed"); sys.exit(2)
target = next((r for r in res if "mann mera" in r["title"].lower()), res[0])
print(f"target: {target['title']} — {target['artist']} ({target['duration']}) {target['videoId']}")

# ---- 2. resolve via in-browser multi-client fetch ----
sig = 0
try:
    sig = engine.it.signature_timestamp() or 0
except Exception:
    pass
print(f"sig_ts={sig}")
r = fastdl.get_direct_streams(target["videoId"], engine._cookie_raw, sig_ts=sig)

print("\n-- client reports --")
for rep in r["reports"]:
    print(f"  {rep['name']:14s} {rep.get('status'):16s} "
          f"formats={len(rep.get('formats') or [])} {rep.get('reason','')[:70]}")

streams = r["streams"]
print(f"\n-- {len(streams)} probe-surviving audio streams --")
for s in streams:
    print(f"  itag={s['itag']:4s} {s['container']:5s} {s['bitrate']:6d}bps "
          f"{s['contentLength']/1e6:7.2f}MB client={s['client']}")
if not streams:
    print("NO PLAIN URLS -> fast path failed"); sys.exit(3)

# ---- 3. parallel download the best stream ----
best = streams[0]
dest = os.path.join(BASE, "downloads", f"fasttest.{best['container']}")
t0 = time.time()
fastdl.parallel_download(best["url"], dest, total=best["contentLength"],
                         progress=lambda n: None, workers=8)
dt = time.time() - t0
size = os.path.getsize(dest)
print(f"\ndownloaded {size/1e6:.2f} MB in {dt:.1f}s -> {size/1e6/max(dt,0.01):.2f} MB/s")
print(f"file: {dest}")

# sanity: full song length?
song_len = int(best.get("durationSec") or 0)
print(f"expected duration {song_len}s, expected size {best['contentLength']/1e6:.2f}MB",
      "| SIZE MATCH" if abs(size - best["contentLength"]) < 1024 else "| SIZE MISMATCH")
sys.exit(0 if size == best["contentLength"] else 4)
