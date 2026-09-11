"""Live end-to-end test: cookies -> ytcfg -> search -> player -> download."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ytm.engine import Engine

BASE = os.path.dirname(os.path.abspath(__file__))
engine = Engine(os.path.join(BASE, "cookies.txt"), os.path.join(BASE, "downloads"))

print("== cookie summary ==")
print(engine.cookie_status())

print("\n== ytcfg ==")
cfg = engine.it.load_ytcfg()
print("client:", cfg.get("INNERTUBE_CLIENT_VERSION"))
print("api_key:", (cfg.get("INNERTUBE_API_KEY") or "")[:20], "...")
print("logged in cookies:", engine.it.has_cookies())

print("\n== search 'mera' ==")
res = engine.search("mera", flt="songs", limit=5)
for i, t in enumerate(res):
    print(f"{i+1}. [{t['type']}] {t['title']} — {t['artist']} ({t['duration']}) {t['videoId']}")
if not res:
    print("NO RESULTS"); sys.exit(1)

target = res[0]
print(f"\n== player for {target['videoId']} ({target['title']}) ==")
t0 = time.time()
info = engine.direct_url(target["videoId"], quality="best")
print("resolved via:", info["source"], f"in {time.time()-t0:.1f}s")
s = info["stream"]
print("stream:", s["itag"], s["container"], s["quality"], s["bitrate"], "bps")
print("track:", info["track"]["title"], "—", info["track"]["author"])

print("\n== downloading ==")
dl_id = engine.start_download(target["videoId"], quality="best")
for _ in range(180):
    st = engine.downloads[dl_id]
    if st["status"] in ("done", "error"):
        break
    time.sleep(1)
st = engine.downloads[dl_id]
print("status:", st["status"], f"{st['bytesDone']}/{st['bytesTotal']} bytes",
      st.get("error") or st.get("file"))
sys.exit(0 if st["status"] == "done" else 2)
