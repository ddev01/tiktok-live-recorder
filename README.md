<div align="center">

# TikTok Live Recorder 🎥

_TikTok Live Recorder is a tool for recording live streaming TikTok._

[![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.me/tiktokliverecorder)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
[![Licence](https://img.shields.io/github/license/Ileriayo/markdown-badges?style=for-the-badge)](./LICENSE)
[![Stars](https://img.shields.io/github/stars/Michele0303/tiktok-live-recorder?style=for-the-badge)](https://github.com/Michele0303/tiktok-live-recorder/stargazers)
[![Release](https://img.shields.io/github/v/release/Michele0303/tiktok-live-recorder?style=for-the-badge)](https://github.com/Michele0303/tiktok-live-recorder/releases/latest)
[![Docker Pulls](https://img.shields.io/docker/pulls/michele0303/tiktok-live-recorder?style=for-the-badge&logo=docker&logoColor=white)](https://hub.docker.com/r/michele0303/tiktok-live-recorder)

The TikTok Live Recorder is a tool designed to easily capture and save live streaming sessions from TikTok. It records both audio and video, allowing users to revisit and preserve engaging live content for later enjoyment and analysis. It's a valuable resource for creators, researchers, and anyone who wants to capture memorable moments from TikTok live streams.

![preview](https://i.ibb.co/YTHp5DT/image.png)

</div>

## Table of Contents

- [Installation](#installation)
- [Usage](#command-line-usage)
- [Guide](#guide)

## Installation

**Prerequisites:** [Git](https://git-scm.com), [Python 3.11+](https://www.python.org/downloads/), [FFmpeg](https://ffmpeg.org/download.html)

<details>
<summary>Windows 💻</summary>

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>Linux 🐧</summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>macOS 🍎</summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
brew install ffmpeg
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>Android — Termux 📱</summary>

Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) (avoid the Play Store version).

```bash
pkg update && pkg upgrade
pkg install git ffmpeg uv tur-repo
pkg uninstall python
pkg install python3.11
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>Docker 🐳</summary>

```bash
sudo docker run \
  -v ./output:/output \
  michele0303/tiktok-live-recorder:latest \
  -output /output \
  -user <username>
```

</details>

## Web Dashboard

This fork adds a private dashboard for starting and managing recordings. It uses Streamlink for TikTok capture, FFmpeg stream-copy remuxing, a local SQLite database, and the browser's built-in video player. Live preview is not included. A broken active stream is saved as an interrupted partial recording; Streamlink does not safely resume an active FLV stream, so the app does not stitch reopened streams together.

For local development, install the Python web extra and build the dashboard assets:

```bash
uv sync --extra web
bun install --frozen-lockfile
bun run build
```

Run the web app and recording worker in separate terminals:

```bash
PYTHONPATH=src uv run gunicorn --workers 3 --bind 127.0.0.1:8000 web.app:app
PYTHONPATH=src uv run python -m web.worker
```

For local UI work (template/CSS reload):

```bash
bun run dev
```

That runs Tailwind in watch mode, Flask on `http://127.0.0.1:8000` (template/`app.js` reload), and the recording worker so queued captures leave `starting`. CSS changes rebuild automatically and the page refreshes when `app.css` updates.

The app stores its SQLite state and recordings under `state/` and `output/` by default. Set `TT_CLIPPER_DATABASE` and `RECORDINGS_DIR` to change those paths. To use a TikTok session cookie, set `TIKTOK_SESSION_SS_FILE` to a Netscape-format cookie file; keep that file out of Git and do not share it. Never paste the cookie into the dashboard.

The Docker Swarm deployment is documented in [docs/deploy.md](docs/deploy.md). It uses the existing Traefik IP allowlist and requires the `tiktok_session_ss` Swarm secret to be provisioned separately.

### Browser Session Reuse (Playwright)

`web.session` loads the same Netscape cookie file into Playwright, so a session created by logging in manually in your browser can be reused. It does not perform password login.

```bash
uv sync --extra web --extra browser
uv run playwright install chromium
PYTHONPATH=src TIKTOK_SESSION_SS_FILE=./cookies.txt uv run python -m web.session
```

The check fails with a re-export prompt if the file is missing, has no TikTok session cookie, or TikTok redirects its auth-only `/messages` check to `/login`. To run the live room test against a room that is currently live:

```bash
PYTHONPATH=src TIKTOK_SESSION_SS_FILE=./cookies.txt TIKTOK_LIVE_URL=https://www.tiktok.com/@creator/live \
  uv run pytest tests/web/test_session.py -k live_room
```

Set `TIKTOK_HEADED=1` to watch the browser.

## Command-Line Usage

```bash
uv run python src/main.py [options]
```

### Options

| Flag | Description |
|------|-------------|
| `-user <USERNAME>` | Username(s) to record. Separate multiple with commas. |
| `-url <URL>` | TikTok live URL to record from. |
| `-room_id <ROOM_ID>` | Room ID to record from. |
| `-mode <MODE>` | Recording mode: `manual`, `automatic`, `followers`. |
| `-automatic_interval <MIN>` | Polling interval in minutes (automatic mode only). |
| `-output <DIRECTORY>` | Directory where recordings will be saved. |
| `-duration <SECONDS>` | Stop recording after this many seconds. |
| `-proxy <URL>` | HTTP proxy to bypass regional restrictions. |
| `-bitrate <BITRATE>` | Output bitrate for post-processing (e.g. `1M`, `1000k`). |
| `-telegram` | Upload the recording to Telegram when done. Requires `telegram.json`. |
| `-no-update-check` | Skip the automatic update check on startup. |

### Recording Modes

- **`manual`** *(default)*: Records immediately if the user is currently live.
- **`automatic`**: Polls at regular intervals and records whenever the user goes live.
- **`followers`**: Automatically records live streams from all followed users.

## Guide

- [How to set cookies in cookies.json](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-set-cookies)
- [How to get room_id](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-get-room_id)
- [How to enable upload to Telegram](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-enable-upload-to-telegram)

## Contributing

Contributions are welcome! Feel free to open an [issue](https://github.com/Michele0303/tiktok-live-recorder/issues) or submit a [pull request](https://github.com/Michele0303/tiktok-live-recorder/pulls).

## Legal ⚖️

This code is in no way affiliated with, authorized, maintained, sponsored or endorsed by TikTok or any of its affiliates or subsidiaries. Use at your own risk.
