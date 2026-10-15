# Clipdeck

A ranking-Shorts studio that runs on your own Windows PC: write rankings with Claude, pull clips from TikTok, YouTube, Instagram and Facebook links, cut them, style the headline and export 1080×1920 MP4s.

## Install

Download `Clipdeck.cmd` from this repo and double-click it. It installs everything it needs and adds a Clipdeck icon to your desktop.

## Updates

Clipdeck checks this repo when it starts and every 6 hours. When `version.json` here is newer than the copy installed on your PC, it downloads the files listed in it, checks each one's SHA-256, and reloads itself. Turn this off in **Settings → Updates**.

## Files

| File | What it is |
|---|---|
| `server.py` | The local server (runs on 127.0.0.1 only) |
| `static/index.html` | The editor |
| `requirements.txt` | Python packages it needs |
| `version.json` | The current version and the SHA-256 of every file |
