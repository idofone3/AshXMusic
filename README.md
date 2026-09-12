<p align="center"><img src="assets/banner_wide.png" alt="AshXMusic" width="100%"></p>

<h1 align="center">AshXMusic 🎵</h1>

<p align="center"><b>24/7 Telegram radio on a YouTube Music queue — with a full
remote control right in the chat.</b></p>

---

## What it is

AshXMusic streams a non-stop music radio to any RTMP(S) endpoint (built for
**Telegram's infinite RTMP**). It plays like YT Music: you send a song name in
the chat, it searches YouTube Music, downloads the full track (own InnerTube +
SABR implementation — no yt-dlp), renders a **1280×720 station frame** (blurred
cover background, square cover art, LIVE badge, title/artist, **64-segment
progress tracker**), and pushes it live. When the queue drains, **autoplay**
keeps picking clean hits.

The stream is tuned **against viewer buffering**: true CBR
(`nal-hrd=cbr`, minrate=maxrate=bufsize=2400k), `-tune zerolatency` (no
lookahead, no B-frames), CFR 30fps with a 2s GOP, 128k AAC — steady bitrate
in, zero re-encoding on the push path (pre-rendered MPEG-TS remuxed through a
FIFO into a single long-lived `ffmpeg -c copy` push).

## The chat is the remote

| | |
|---|---|
| **Listening** | send any **song name** (plays next) • `/play` • `/add` • `/search` (tap-to-queue) |
| **Playback** | `⏸ Pause` / `▶ Resume` / `⏭ Skip` **buttons** • `/pause` `/resume` `/skip` `/np` |
| **Modes** | 🔁 `/loop off\|one\|all` • 🔀 `/shuffle` • 🤖 `/autoplay on\|off` • `/volume 0-150` |
| **Queue** | `/queue` (with ❌ per-track remove + shuffle/clear buttons) • `/history` |
| **Stream ops** | `/configure <server> <key>` — saves creds + auto-restarts the workflow • `/config` • `/status` • `/stats` • `/stopstream` |
| **Misc** | `/id` `/ping` `/help` |

The **Now-Playing panel** is live: cover art, ▰▱ progress bar, elapsed %
and the full button pad, edited in place every ~8s. Pause keeps the stream
alive with a "PAUSED" slate and **resumes mid-song** (input-seek remux).

## Deploy (2 minutes)

1. **Fork or use as-is** → repo *Settings → Secrets and variables → Actions*:

   | Secret | Meaning |
   |---|---|
   | `RTMP_URL` | e.g. `rtmps://dc5-1.rtmp.t.me/s/` |
   | `RTMP_KEY` | e.g. `3131932193:AbCdEf…` |
   | `TG_BOT_TOKEN` | bot that controls the radio (optional) |
   | `TG_CHAT_ID` | control chat (group or you) |
   | `GH_PAT` | PAT with `repo` + `workflow` scope (chain + `/configure`) |
   | `COOKIES_B64` | base64 of cookies.txt (YouTube login for downloads) |

2. **Actions → AshXMusic 24/7 Radio → Run workflow** → enter `rtmp_url` +
   `rtmp_key` (+ first song if you want) → **Run**.
3. The run chains itself every ~5.5h → **24/7**. Stop anytime with
   `stop=true` or `/stopstream`. Change server live with
   `/configure rtmps://… key`.

No secrets yet? Run with the inputs only — creds are persisted for the chain
automatically. Queue state survives restarts (pushed to the `radio-state`
branch); song cache rides on `actions/cache`.

## The stack

```
radio/
├── station.py      24/7 supervisor: downloader → renderer → pump → push
├── visuals.py      ffmpeg frame builder (blur bg, cover, tracker, slates)
├── pool.py         curated Hindi-hits autoplay pool (remix/cover filtered)
├── stateio.py      queue persistence + radio-state branch sync
├── bot/            the Telegram remote (modular)
│   ├── api.py         Bot API client (incl. setMyName, rich messages)
│   ├── handlers.py    every command + button callback
│   ├── keyboards.py   inline pads (play/pause/skip/loop/shuffle/autoplay)
│   ├── panel.py       live now-playing panel (progress edits)
│   ├── configure.py   /configure → gh secret set → graceful restart
│   └── bot.py         polling thread + identity setup
ytm/               InnerTube/SABR engine (search, download, MP4 + cover)
scripts/           chrome installer, tests, banner generator
.github/workflows/radio.yml   the 24/7 chain
```

**Download pipeline** (in `ytm/`): WEB_REMIX InnerTube → SABR session minted
in a real Chrome (persistent Xvfb) with a PO token → python-direct googlevideo
fetch (~55 MB/s) → MP4 with embedded cover → rendered to the station frame.

---

<p align="center"><sub>Built from scratch — no yt-dlp, no pytube. Just
InnerTube, SABR, ffmpeg and spite. ⭐ if it plays your song.</sub></p>
