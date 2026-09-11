"""API smoke test: boots the FastAPI server, exercises all endpoints,
downloads a track through POST /downloads and verifies the file."""
import json
import os
import subprocess
import sys
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DL = os.path.join(BASE, "downloads")
proc = subprocess.Popen([sys.executable, "run.py"], cwd=BASE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    base = "http://127.0.0.1:8000"
    for _ in range(30):
        try:
            requests.get(base + "/health", timeout=2)
            break
        except Exception:
            time.sleep(1)
    print("health:", requests.get(base + "/health", timeout=5).json())

    r = requests.get(base + "/search", params={"q": "mann mera gajendra verma",
                                              "filter": "songs", "limit": 5},
                     timeout=30).json()
    results = r["results"]
    target = next((x for x in results if "mann mera" in x["title"].lower()), results[0])
    print(f"search ok: {target['title']} — {target['artist']} {target['videoId']}")

    t0 = time.time()
    dl = requests.post(base + f"/downloads/{target['videoId']}",
                       params={"quality": "best"}, timeout=30).json()
    dl_id = dl["downloadId"]
    print("download started:", dl_id)

    last = None
    while time.time() - t0 < 420:
        st = requests.get(base + f"/downloads/{dl_id}", timeout=10).json()
        cur = (st["status"], st["bytesDone"])
        if cur != last:
            print(f"  {time.time()-t0:6.1f}s  {st['status']:10s} "
                  f"{st['bytesDone']}/{st['bytesTotal']}")
            last = cur
        if st["status"] in ("done", "error"):
            break
        time.sleep(2)
    st = requests.get(base + f"/downloads/{dl_id}", timeout=10).json()
    dt = time.time() - t0
    print("FINAL:", st["status"], "| mode:", st.get("mode"),
          "| file:", st.get("filename"), f"| {dt:.1f}s")
    if st["status"] != "done":
        sys.exit(2)

    f = requests.get(base + f"/downloads/file/{dl_id}", timeout=60)
    out = os.path.join(DL, "api_test_download.webm")
    open(out, "wb").write(f.content)
    print(f"fetched via API: {len(f.content)} bytes -> {out}")
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration,size", "-of", "json", out],
                       capture_output=True, text=True)
    print("ffprobe:", p.stdout.strip() or p.stderr[:150])
    print("\nlist:", json.dumps(
        [d.get("status") for d in
         requests.get(base + "/downloads", timeout=10).json()["downloads"]]))
    print("API SMOKE TEST PASS")
finally:
    proc.terminate()
