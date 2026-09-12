# AshXMusic API — Render-ready image (free tier friendly: no volumes/disks)
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# xvfb    -> persistent virtual display (headed Chrome = trusted pot tokens)
# ffmpeg  -> mp4 mux + embedded cover art (postprocess.py uses ffmpeg+ffprobe)
# libs    -> Chrome-for-Testing runtime dependencies (bookworm names)
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth ffmpeg ca-certificates curl unzip fonts-liberation \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
        libglib2.0-0 libx11-6 libxcb1 libxext6 libxi6 libxtst6 \
        libexpat1 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Bake Chrome-for-Testing + matching chromedriver into the image so cold
# starts (free tier spins down after 15 min idle) never download anything.
COPY scripts/install_cft_chrome.py /tmp/install_cft_chrome.py
RUN python /tmp/install_cft_chrome.py && rm -f /tmp/install_cft_chrome.py

COPY . .

# Ephemeral storage only — Render free tier has no persistent disks.
# Everything (mp4s, telegram config, cookies) lives in the container FS.
ENV YTM_DL_DIR=/tmp/downloads \
    PATH="/root/.local/bin:${PATH}"

EXPOSE 10000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000}"]
