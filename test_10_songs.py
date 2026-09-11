"""Speed test: auto-configures the Telegram bot, then downloads 10 Hindi
hits (none of the 3 already sent) one by one, each auto-pushed to the
group as a tagged MP4 with cover art. Prints per-song + total speed table.

Run:  python3 test_10_songs.py
"""
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ytm.engine import Engine
from ytm import telegram as ytg

BASE = os.path.dirname(os.path.abspath(__file__))
DL = os.path.join(BASE, "downloads")

BOT_TOKEN = "os.environ.get("TELEGRAM_BOT_TOKEN", "")"
CHAT_ID = "os.environ.get("TELEGRAM_CHAT_ID", "")"

# (search query, title keyword, artist keyword) — Hindi hits, excluding
# the 3 already sent (Kesariya / Apna Bana Le / Mann Mera)
SONGS = [
    ("raataan lambiyan shershaah jubin nautiyal", "lambiyan", "jubin"),
    ("tum hi ho aashiqui 2 arijit singh", "tum hi ho", "arijit"),
    ("channa mereya ae dil hai mushkil arijit singh", "channa mereya", "arijit"),
    ("agar tum saath ho tamasha arijit singh", "agar tum saath ho", "arijit"),
    ("khairiyat chhichhore arijit singh", "khairiyat", "arijit"),
    ("shayad love aaj kal arijit singh", "shayad", "arijit"),
    ("bekhayali kabir singh sachet tandon", "bekhayali", "sachet"),
    ("tera ban jaunga kabir singh akhil sachdeva", "ban jaunga", "akhil"),
    ("kabira yeh jawaani hai deewani", "kabira", ""),
    ("tum se hi jab we met mohit chauhan", "tum se hi", "mohit"),
]

API = f"https://api.telegram.org/bot{BOT_TOKEN}/{{m}}"


def existing_video_ids():
    ids = set()
    if os.path.isdir(DL):
        for fn in os.listdir(DL):
            m = re.search(r"\[([0-9A-Za-z_-]{11})\]\.mp4$", fn)
            if m:
                ids.add(m.group(1))
    return ids


def tg_text(text):
    try:
        requests.post(API.format(m="sendMessage"), timeout=20,
                      json={"chat_id": CHAT_ID, "text": text})
    except Exception:
        pass


def main():
    # 1) auto-configure telegram log (persisted, API server uses it too)
    eng = Engine(os.path.join(BASE, "cookies.txt"), DL)
    eng.allow_capture = False   # speed test: no realtime fallback tier
    log = ytg.get_logger(eng.base_dir)
    log.set(BOT_TOKEN, CHAT_ID, enabled=True)
    print("telegram configured:", log.status(), flush=True)

    skip = existing_video_ids()
    print(f"already downloaded (skip): {len(skip)} videos\n", flush=True)

    tg_text("🚀 Speed test started — 10 Hindi hits incoming, one by one.")

    results = []
    dl_ids = {}
    for i, (query, kw, akw) in enumerate(SONGS, 1):
        print(f"—— [{i}/10] {query}", flush=True)
        try:
            res = eng.search(query, flt="songs", limit=8)
        except Exception as e:
            print(f"   search failed: {e}", flush=True)
            results.append((query, "search-fail", 0, 0, None))
            continue
        def _t(r):
            t = r["title"].lower()
            if any(w in t for w in ("remix", "lofi", "slowed", "reverb",
                                    "cover", "instrumental", "karaoke",
                                    "nightcore", "8d", "sped up",
                                    "reprise", "acoustic", "encore",
                                    "unplugged", "live")):
                return False
            return kw in t

        def _a(r):
            return (not akw) or akw in (r.get("artist") or "").lower()

        hits = [r for r in res if _t(r) and _a(r)]
        if not hits:
            hits = [r for r in res if _t(r)]
        if not hits:
            print("   no match found", flush=True)
            results.append((query, "no-match", 0, 0, None))
            continue
        # a song already on disk counts as done — never pick variant
        # uploads (remix/acoustics/2nd channel) of an already-done song
        if any(r["videoId"] in skip for r in hits):
            print(f"   already downloaded — skipping {hits[0]['title']}",
                  flush=True)
            results.append((hits[0]["title"], "already", 0, 0, None))
            continue
        cand = hits[0]
        vid = cand["videoId"]
        skip.add(vid)
        print(f"   → {cand['title']} — {cand.get('artist', '')}  ({vid})",
              flush=True)

        t0 = time.time()
        dl_id = eng.start_download(vid)
        last, stages = None, {}
        deadline = t0 + 150
        while time.time() < deadline:
            st = eng.downloads.get(dl_id, {})
            cur = st.get("status")
            if cur != last:
                stages[cur] = time.time()
                print(f"   {time.time()-t0:6.1f}s  {cur}", flush=True)
                last = cur
            if cur in ("done", "error"):
                break
            time.sleep(0.4)
        total = time.time() - t0
        st = eng.downloads.get(dl_id, {})
        if st.get("status") != "done":
            err = st.get("error") or st.get("tierErrors") or "timeout"
            print(f"   ✗ FAILED: {err}", flush=True)
            results.append((cand["title"], "fail", 0, total, None))
            continue
        mb = (st.get("bytesDone") or st.get("bytesTotal") or 0) / 1e6
        tg = st.get("telegram") or {}
        print(f"   ✓ {mb:.2f} MB ready in {total:.1f}s "
              f"({mb/max(total,.1):.2f} MB/s) mode={st.get('mode')} "
              f"durationOk={st.get('durationOk')} "
              f"tg={tg.get('via', '?')} (upload runs in background)",
              flush=True)
        results.append((cand["title"], st.get("mode"), mb, total, tg))
        dl_ids[len(results) - 1] = dl_id

    # wait for the background telegram pushes, then refresh their status
    eng.wait_telegram(timeout=300)
    for idx, dl_id in dl_ids.items():
        st = eng.downloads.get(dl_id, {})
        title, mode, mb, sec, tg = results[idx]
        results[idx] = (title, mode, mb, sec, st.get("telegram") or tg)

    # summary
    okr = [r for r in results if r[2] > 0]
    done = [r for r in results if r[1] == "already"]
    tot_mb = sum(r[2] for r in okr)
    tot_s = sum(r[3] for r in okr)
    print("\n===== SPEED SUMMARY =====", flush=True)
    for title, mode, mb, sec, tg in results:
        if mb > 0:
            print(f"  {title[:38]:40s} {str(mode):8s} {mb:6.2f}MB "
                  f"{sec:6.1f}s {mb/max(sec,.1):5.2f}MB/s "
                  f"tg={tg.get('via', '?') if tg else '-'}", flush=True)
        else:
            print(f"  {title[:38]:40s} {mode}", flush=True)
    avg = tot_s / max(len(okr), 1)
    print(f"\nTOTAL: {len(okr)} downloaded + {len(done)} already done "
          f"= {len(okr) + len(done)}/10 | {tot_mb:.1f} MB in {tot_s:.0f}s "
          f"→ avg {avg:.1f}s/song (mp4 incl.; telegram uploads overlapped "
          f"in background)",
          flush=True)
    tg_text(f"✅ Speed test finished: {len(okr) + len(done)}/10 songs "
            f"({tot_mb:.0f} MB in {tot_s:.0f}s, avg {avg:.0f}s per song — "
            f"telegram uploads now run in background, off the critical path)")
    return 0 if len(okr) + len(done) == len(SONGS) else 1


if __name__ == "__main__":
    sys.exit(main())
