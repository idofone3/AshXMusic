# AshXMusic API 🎵

Personal **YouTube Music search / stream / download API** — pure Python +
SeleniumBase, **no premade YT download libraries** (InnerTube + SABR
reverse-engineered from scratch). Every download is an **mp4 with embedded
cover art**; optional **Telegram auto-push** for finished songs.

Ships with a **minimal dark web UI** (phone-friendly) on the homepage:
search → tap to play → download with live progress → save the mp4.

**Hosted 24/7 on GitHub Actions runners, exposed worldwide through your own
Cloudflare Tunnel** (`host.yml` workflow — cookies preinitialised from
secrets, self-chaining every ~5.5h, never hits the 6h job limit). Render
blueprint still included as an alternative.

---

## Web UI (homepage `/`)

- **Search** YouTube Music (songs / videos / albums / all chips) — the top
  results are **pre-downloaded in the background** while you browse, so
  tapping play is usually **instant**
- **Tap a result to play** — cached songs start instantly (seekable 206);
  a song that isn't cached yet auto-downloads first (~1min, one time) and
  every play after that is instant from cache
- **⤓ button** downloads with a live % progress; when done it becomes a
  green **save** link (and the song is pushed to Telegram if configured)
- Sticky player bar: play/pause, seek, elapsed/total
- Header status dot: green = logged-in cookies OK, amber = no cookies,
  red = server offline

## Endpoints

| Method | Path | What it does |
|--------|------|--------------|
| GET  | `/health` | liveness + cookie / telegram status |
| GET  | `/search?q=...&filter=songs&limit=20` | YT Music search (all / songs / videos / albums / artists / playlists) |
| GET  | `/track/{videoId}` | resolve streams + metadata (no download) |
| GET  | `/stream/{videoId}` | audio: cached mp4 (instant, seekable) first, else 302/proxy. `?mint=1` = JSON api for the web UI (cached url / 202) |
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

### Getting the cookies (what to enter)

1. Open **music.youtube.com** in Chrome/Firefox and make sure you're
   **logged in** (the account with your library/likes).
2. Export the cookies in **Netscape format** — easiest with the extension
   *"Get cookies.txt LOCALLY"* (Chrome) or *"cookies.txt"* (Firefox) while
   on music.youtube.com.
3. The critical cookies are `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`,
   `__Secure-1PSID`, `__Secure-3PSID` (+ their `__Secure-*PSIDTS` twins).
   If those are present, you're good.
4. Locally: save as `cookies.txt` next to `server.py`.
   On Render: set env var **`COOKIES_B64`** to `base64 -w0 cookies.txt`.

Cookies expire occasionally (weeks/months) — if the UI dot turns amber or
logged-in tracks stop resolving, re-export and update `COOKIES_B64`.

## Host on GitHub Actions (free, via your Cloudflare Tunnel)

`.github/workflows/host.yml` turns the repo into a self-hosting machine:

1. **Secrets** (Settings → Secrets and variables → Actions) — all already
   set for this repo:
   - `COOKIES_B64` — `base64 -w0 cookies.txt` output
   - `TUNNEL_TOKEN` — Zero Trust → Networks → Tunnels → your tunnel →
     "Install and run a connector" → copy the `eyJ...` service token
   - `TG_BOT_TOKEN` / `TG_CHAT_ID` — optional Telegram auto-push
2. **Public hostname**: Zero Trust → tunnel → Public Hostname → Service =
   `HTTPS://localhost:8080` (the workflow generates a self-signed cert and
   trusts it in the runner's CA store automatically).
3. **Run**: Actions → *host* → **Run workflow**. The runner installs deps +
   Chrome + ffmpeg, restores cookies, serves `https://localhost:8080` over
   TLS and connects `cloudflared` — your hostname is live worldwide.
4. **Self-chaining**: a watchdog dispatches a fresh run before GitHub's 6h
   job ceiling (~5.5h cycles, near-zero gap); a 6h cron is the safety net.
   Redeploy = cancel the running job, then Run workflow again.

Why songs are pre-downloaded instead of streamed directly: Google
throttles/bot-walls media byte fetches from datacenter IPs, and SABR urls
are UMP-framed (unplayable by plain `<audio>`). The API therefore
pre-downloads search results (fast in-browser SABR) and plays from its
seekable cache — that's what makes playback instant.

## Deploy on Render (free tier — alternative)

The repo ships `render.yaml` + `Dockerfile` (Render Blueprint).

1. Push this repo to GitHub.
2. Render dashboard → **New + → Blueprint** → select the repo → **Apply**.
3. After it deploys open **Environment** and add `COOKIES_B64` (=
   `base64 -w0 cookies.txt` output). The service redeploys with cookies —
   status dot on the homepage turns green.
4. Optional: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to auto-push every
   downloaded song to your Telegram chat.

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
