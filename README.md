# 🎬 YouTube Video & Shorts Downloader

Download YouTube videos and Shorts as MP4 — paste a link, hit Download. No accounts, no tracking.

**Use it here:** https://askarali2024.github.io/youtube-fetcher/

## How to use

1. Copy the link of a YouTube video or Short, e.g. `https://www.youtube.com/watch?v=dQw4w9WgXcQ`, `https://www.youtube.com/shorts/zss_cbGqA_g`, or a share link like `https://youtu.be/dQw4w9WgXcQ`.
2. Paste it into the URL field.
3. Click **Download**, preview the video, then click **Save to your device**.

## How it works

The app is a **single static page** (`index.html`) — exactly like the [snake game](https://github.com/askarali2024/snake-game). It runs entirely in your browser: the page sends your URL to free community [cobalt](https://github.com/imputnet/cobalt) processing instances (open, no-sign-up APIs), which fetch the video from YouTube and stream the MP4 back to you. If one instance is rate-limited or blocked, the page automatically tries the next.

Because there's no backend of our own, GitHub Pages can host the whole app for free.

### Deployment (GitHub Pages)

1. Push this repo to GitHub.
2. In the repo: **Settings → Pages → Source: Deploy from a branch** → branch `main`, folder `/ (root)`.
3. Your app is live at `https://<username>.github.io/<repo-name>/`.

That's the entire deployment — no build step, no server, no keys.

### Advanced — custom processing server

The community instances that power downloads are run by volunteers and occasionally get rate-limited or blocked by YouTube. If downloads fail:

- Open **Advanced → processing server** on the page and paste any cobalt instance URL (it's tried first, stored only in your browser), or
- Self-host your own instance of [cobalt](https://github.com/imputnet/cobalt) and paste its API URL there.

## Also in this repo (optional, for local/self-hosted use)

| Path | What it is | When to use |
|---|---|---|
| `index.html` | Static GitHub Pages app | **Default — share the Pages link.** |
| `web/` | Flask + yt-dlp backend (self-host) | You want downloads processed by your own server instead of community instances. |
| `app.py` + `requirements.txt` | Streamlit single-file app | Quick local personal use from the terminal. |

### Flask version (`web/`)

Your own processing server using yt-dlp + FFmpeg (system install, or bundled via `imageio-ffmpeg`):

```bash
cd web
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # http://localhost:8000
```

For a persistent deployment use one worker (jobs live in-process): `gunicorn -w 1 -b 0.0.0.0:8000 app:app`

API: `POST /api/download` → poll `GET /api/progress/<id>` → fetch `GET /api/file/<id>`.

### Streamlit version (root)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py   # http://localhost:8501
```

## Notes

- The static app depends on third-party community servers — if all of them fail at once, wait a few minutes or add a custom instance.
- The Python versions need `pip install -U yt-dlp` occasionally, since yt-dlp tracks YouTube's site changes.
- Only download content you own or have permission to use. Respect YouTube's Terms of Service and applicable copyright law.

## Project layout

```
youtube-fetcher/
├── index.html              ← the web app (GitHub Pages serves this)
├── README.md
├── app.py                  ← Streamlit version (local)
├── requirements.txt        ← Streamlit deps
└── web/                    ← optional self-hosted backend
    ├── app.py              ← Flask + yt-dlp download server
    ├── requirements.txt
    └── templates/index.html
```
