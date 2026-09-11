# 📻 YTM Telegram Radio — 24/7 YouTube Music → Telegram RTMP

Streams a Hindi-hits radio **24/7** to any RTMP(S) target (Telegram live
channels, YouTube, etc.) with a YT-Music-style **queue** and a clean
**on-screen UI**: blurred cover background, floating thumbnail, live title /
artist, elapsed-time clock and an animated progress tracker.

```
tgbot ──requests──┐
pool ──autoplay───┤   [downloader]  radio/cache/*.mp4  (SABR, python-direct)
                  ▼        ▼
          [renderer]  radio/rendered/*.ts  (blur bg + cover + tracker)
                  ▼
          [pump]  remux → running-timestamp FIFO (gap-free)
                  ▼
          [streamer]  ffmpeg -re → rtmps://…t.me/s/<key>
```

- **No premade download libs** — the InnerTube/SABR engine in `ytm/` is
  reverse-engineered from scratch (SeleniumBase mints the session, data is
  fetched python-direct at ~55 MB/s).
- **Gap-free**: songs are pre-rendered to MPEG-TS and pumped into one
  long-lived ffmpeg push, so the live stream never reconnects per song.
- **A 20s idle slate** fills any download gap — never a black frame.
- **Queue like YT Music**: user requests jump to the front ("play next"),
  then the autoplay mix (110+ curated Hindi hits in `radio/pool.py`)
  keeps going forever.

## 🚀 Run it (GitHub Actions)

1. Repo **Actions → 24/7 Radio → Run workflow**
2. Fill in:
   - `rtmp_url` — e.g. `rtmps://dc5-1.rtmp.t.me/s/`
   - `rtmp_key` — e.g. `3131932193:pOcW0qdposGDcAO50qJJbg`
   - `song` — first song (optional), `hours` — run length (≤5.7)
3. The first run **saves the creds as repo secrets** and, with `chain=true`
   (default), automatically starts the next run when it ends → true 24/7.

**Stop**: run the workflow with `stop=true`. **Resume**: any run with
`stop=false`. The queue survives restarts (pushed to the `radio-state`
branch) and downloaded songs are cached between runs.

> Public repo → Actions minutes are free. Keep it public and never commit
> cookies/keys (everything is passed via secrets).

## 🎛 Telegram remote control

The radio posts **Now playing** (with cover) into your control chat and
accepts commands (only from `TG_CHAT_ID`):

| Message | Action |
|---|---|
| any song name (plain text) | 🔥 plays it **next**, then continues the radio |
| `/play <song>` | same as above |
| `/add <song>` | append to queue |
| `/skip` | cut the current song |
| `/np` | now playing + elapsed |
| `/queue` | upcoming (⭐ = user request) |
| `/help` | commands |

## 🖥 Run locally

```bash
pip install -r requirements.txt
python scripts/install_cft_chrome.py          # Chrome for Testing
export RTMP_URL=rtmps://dc5-1.rtmp.t.me/s/ RTMP_KEY=<key>
export TG_BOT_TOKEN=<bot token> TG_CHAT_ID=<chat id>
python radio/station.py --hours 6 --song "Kesariya" --rtmp "$RTMP_URL" --key "$RTMP_KEY"
```

Cookies go to `data/cookies.txt` (Netscape format).

## 🧩 Layout

| Path | What |
|---|---|
| `radio/station.py` | orchestrator: queue, threads, FIFO pump, streamer |
| `radio/visuals.py` | ffmpeg filtergraph (blur bg, cover, clock, 64-seg tracker) |
| `radio/pool.py` | 110+ Hindi hit seeds + remix/cover filter |
| `radio/tgbot.py` | long-poll control bot |
| `radio/stateio.py` | queue persistence + `radio-state` branch sync |
| `ytm/` | reverse-engineered InnerTube/SABR engine (no yt-dlp) |
| `server.py`, `run.py` | the original download API (FastAPI) + bot |

Also part of this repo: the standalone **YTM download API** (`server.py`,
`/search`, `/download`, Telegram bot with rich 10.x messages) — see
`ytm/` engine docs and `test_*.py` examples.
