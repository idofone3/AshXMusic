# AshXMusic API 🎵

Personal **YouTube Music search / stream / download API** — pure Python +
SeleniumBase, **no premade YT download libraries** (InnerTube + SABR
reverse-engineered from scratch). Every download is an **mp4 with embedded
cover art**; optional **Telegram auto-push** for finished songs.

One-click deployable to **Render (free tier)** — no disks, no paid features.

---

## Endpoints

| Method | Path | What it does |
|--------|------|--------------|
| GET  | `/health` | liveness + cookie / telegram status |
| GET  | `/search?q=...&filter=songs&limit=20` | YT Music search (all / songs / videos / albums / artists / playlists) |
| GET  | `/track/{videoId}` | resolve streams + metadata (no download) |
| GET  | `/stream/{videoId}?quality=best&redirect=false` | proxy (or 302 → googlevideo) the audio stream |
| POST | `/downloads/{videoId}?quality=best` | start async download → `{"downloadId", "statusUrl"}` |
| GET  | `/downloads/{dlId}` | progress: status, bytes, errors, mp4 info |
| GET  | `/downloads/file/{dlId}` | fetch the finished mp4 |
| GET  | `/downloads` | list all downloads this boot |
| POST | `/cookies` | live-update the YT Music cookie (netscape / json / header) |
| POST | `/telegram` | configure auto-push (bot token + chat id) |
| GET  | `/telegram` · `/telegram/test` | status / send a test song-less message |

Interactive docs at `/docs` (Swagger) once the server is running.

## Download pipeline (how it beats bot-walls)

1. **SABR in-browser** (tier 1): a persistent, headed UC Chrome (real
   fingerprint + your cookies, on Xvfb) opens YT Music; the song's SABR
   stream is fetched straight from googlevideo by Python at full speed
   (~50 MB/s). Retries ride out YouTube's control-parts-only throttle
   bursts.
2. **Direct InnerTube clients** (tier 2): yt-dlp-style client hopping
   (ANDROID_VR / IOS / ...) fired from inside the real page, then parallel
   HTTP range downloads.
3. **Realtime capture** (tier 3, last resort): MediaRecorder capture of the
   playing track.

The raw audio is muxed to mp4 with ffmpeg, the cover art is embedded, the
duration is sanity-checked, and the result is pushed to Telegram (if
configured) in a background thread.

---

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 scripts/install_cft_chrome.py   # Chrome-for-Testing + chromedriver
sudo apt install xvfb ffmpeg            # virtual display + muxer (debian/ubuntu)
python3 run.py                          # http://localhost:8000/docs
```

`cookies.txt` (YT Music cookies, Netscape format) is picked up automatically.
Without it, search still works but logged-in-only tracks won't resolve.

## Deploy on Render (free tier — blueprint)

The repo ships `render.yaml` + `Dockerfile` (Render Blueprint).

1. Push this repo to GitHub.
2. Render dashboard → **New + → Blueprint** → select the repo → **Apply**.
3. Render builds the Docker image (Xvfb + ffmpeg + Chrome baked in) and
   starts the API on your free instance. Health check: `/health`.

### Environment variables (set in Render → Environment)

| Var | Required | Purpose |
|-----|----------|---------|
| `COOKIES_B64` | recommended | base64 of your `cookies.txt` — restored at boot on the ephemeral FS. Export with `base64 -w0 cookies.txt` |
| `TELEGRAM_BOT_TOKEN` | optional | auto-push finished songs to Telegram |
| `TELEGRAM_CHAT_ID` | optional | target chat (e.g. `-1004479642453`) |
| `YTM_NO_TG` | optional | `1` disables Telegram pushes entirely |
| `YTM_DL_DIR` | optional | override downloads dir (default `/tmp/downloads`) |

### Free-tier behaviour (read this)

- **No disks** — Render disks are paid-only. Everything lives in the
  ephemeral container FS: downloads go to `/tmp/downloads`, the Telegram
  config to `data/telegram.json`. **Finished files survive only until the
  next redeploy/spin-down** — fetch via `/downloads/file/{id}` or push to
  Telegram promptly.
- **Spin-down after ~15 min idle** — the next request wakes the service
  (cold start ≈ under a minute; Chrome is pre-baked in the image, nothing
  is downloaded at boot).
- 512 MB RAM: the engine launches one lean Chrome tab
  (`--disable-dev-shm-usage`, no GPU) and recycles it hourly; downloads are
  streamed, not buffered in RAM.

## API usage example

```bash
BASE=https://your-service.onrender.com

# search
curl "$BASE/search?q=kesariya&filter=songs&limit=3"

# download the first result
curl -X POST "$BASE/downloads/$(curl -s "$BASE/search?q=kesariya&limit=1" \
      | jq -r '.results[0].videoId')"

# poll + fetch
curl -s "$BASE/downloads/<downloadId>" | jq .status
curl -o song.m4a "$BASE/downloads/file/<downloadId>"
```
