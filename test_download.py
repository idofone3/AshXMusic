"""Test full download: url-path (likely blocked here) -> capture fallback."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ytm.engine import Engine

BASE = os.path.dirname(os.path.abspath(__file__))
engine = Engine(os.path.join(BASE, "cookies.txt"), os.path.join(BASE, "downloads"))

res = engine.search("mera", flt="songs", limit=10)


def dur(t):
    try:
        p = [int(x) for x in t["duration"].split(":")]
        return p[0] * 60 + p[1] if len(p) == 2 else p[0] * 3600 + p[1] * 60 + p[2]
    except Exception:
        return 9999


short = sorted(res, key=dur)[0]
print(f"target: [{short['type']}] {short['title']} — {short['artist']} ({short['duration']}) {short['videoId']}")

dl_id = engine.start_download(short["videoId"], quality="best")
t0 = time.time()
last = None
while time.time() - t0 < 560:
    st = engine.downloads[dl_id]
    cur = (st["status"], st["bytesDone"])
    if cur != last:
        print(f"  {time.time()-t0:6.0f}s  {st['status']:12s} {st['bytesDone']}/{st['bytesTotal']}")
        last = cur
    if st["status"] in ("done", "error"):
        break
    time.sleep(3)

st = engine.downloads[dl_id]
print("FINAL:", st["status"], "|", st.get("error") or st.get("file"))
sys.exit(0 if st["status"] == "done" else 2)
