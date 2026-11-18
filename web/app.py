"""YouTube Downloader — web version (Flask).

Serves a minimal index page. Downloads run in background threads; the
frontend polls /api/progress/<id> for live progress and fetches the
finished MP4 from /api/file/<id>. Uses yt-dlp, with FFmpeg (system
install, or bundled via imageio-ffmpeg) for merging video + audio.
Works for regular YouTube videos and Shorts.
"""

import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import Flask, abort, jsonify, render_template, request, send_file

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "yt_shorts_downloader_web"
FILE_MAX_AGE_SECONDS = 60 * 30  # purge cached files/jobs older than 30 min

app = Flask(__name__)

# In-memory job store: job_id -> state dict. One worker process only
# (see README) so this is safe without external storage.
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_ffmpeg() -> str | None:
    """Return the path to an ffmpeg binary if available, else None.

    Checks the system PATH first, then falls back to the static ffmpeg
    binary bundled with the imageio-ffmpeg pip package (if installed).
    """
    if path := shutil.which("ffmpeg"):
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def extract_video_id(url: str) -> str | None:
    """Return the 11-character video ID from any common YouTube URL form.

    Accepts: youtube.com/watch?v=<id> (any param order), /shorts/<id>,
    /live/<id>, /embed/<id>, /v/<id>, youtu.be/<id>, on www./m./mobile.
    hosts, with or without a scheme.
    """
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.netloc or "").lower()
    host = re.sub(r"^(www\.|m\.|mobile\.)", "", host)
    id_re = re.compile(r"^[A-Za-z0-9_-]{11}$")

    if host == "youtu.be":
        vid = (parsed.path.lstrip("/").split("/") or [""])[0]
        return vid if id_re.match(vid) else None

    if host == "youtube.com" or host.endswith(".youtube.com"):
        m = re.match(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})(?:[/?#].*)?$", parsed.path)
        if m:
            return m.group(1)
        if parsed.path.rstrip("/") == "/watch":
            vid = (parse_qs(parsed.query).get("v") or [""])[0]
            return vid if id_re.match(vid) else None
    return None


def friendly_error(exc: Exception) -> str:
    """Translate common yt-dlp failures into clear user-facing messages."""
    message = str(exc).lower()
    if "requested format is not available" in message:
        return ("This video offers no downloadable format for the requested "
                "quality. Try updating yt-dlp: pip install -U yt-dlp")
    if "private" in message:
        return "This video is private and cannot be downloaded."
    if "sign in" in message or "login" in message or "cookies" in message:
        return "This video requires sign-in (age-restricted or members-only) and cannot be downloaded."
    if "unavailable" in message or "removed" in message:
        return "This video is unavailable or has been removed."
    if "not a valid url" in message or "unsupported url" in message:
        return "That doesn't look like a valid YouTube URL. Please check it and try again."
    if "getaddrinfo failed" in message or "network" in message or "timed out" in message:
        return "Network error while contacting YouTube. Check your connection and try again."
    return f"Download failed: {exc}"


def safe_percent(data: dict) -> float:
    """Extract a 0.0-1.0 progress fraction from a yt-dlp progress dict."""
    if data.get("total_bytes") is not None:
        return min(data["downloaded_bytes"] / data["total_bytes"], 1.0)
    if data.get("total_bytes_estimate") is not None:
        return min(data["downloaded_bytes"] / data["total_bytes_estimate"], 1.0)
    return 0.0


def purge_expired() -> None:
    """Best-effort cleanup of old jobs and their cached files."""
    now = time.time()
    with jobs_lock:
        expired = [jid for jid, job in jobs.items()
                   if now - job["created_at"] > FILE_MAX_AGE_SECONDS]
        for jid in expired:
            job = jobs.pop(jid)
            path = job.get("file_path")
            if path:
                Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Background download worker
# ---------------------------------------------------------------------------

def run_download(job_id: str, url: str, ffmpeg_path: str | None) -> None:
    """Download the video in this thread and update the job record."""
    job = jobs[job_id]

    def set_state(**kwargs) -> None:
        with jobs_lock:
            job.update(kwargs)

    try:
        video_id = job["video_id"]

        def progress_hook(data: dict) -> None:
            if data["status"] == "downloading":
                set_state(
                    status="downloading",
                    progress=safe_percent(data),
                    detail=(
                        f"{data.get('_percent_str', '?').strip()} "
                        f"at {data.get('_speed_str', '?').strip()}"
                    ),
                )
            elif data["status"] == "finished":
                set_state(status="merging", progress=1.0, detail="Merging streams…")

        ydl_opts: dict = {
            "outtmpl": str(DOWNLOAD_DIR / f"{video_id}.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "restrictfilenames": True,
        }

        if ffmpeg_path:
            ydl_opts.update({
                "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "merge_output_format": "mp4",
                "ffmpeg_location": ffmpeg_path,
            })
        else:
            ydl_opts["format"] = "b[ext=mp4]/b"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        vid = info.get("id", video_id)
        candidates = sorted(DOWNLOAD_DIR.glob(f"{vid}.*"),
                            key=lambda p: p.stat().st_mtime)
        mp4s = [p for p in candidates if p.suffix == ".mp4"]
        chosen = mp4s[-1] if mp4s else (candidates[-1] if candidates else None)

        if chosen is None or not chosen.exists() or chosen.stat().st_size == 0:
            raise FileNotFoundError("Download finished but no output file was produced.")

        # Human-friendly filename derived from the video title.
        title = re.sub(r"[^\w\- ]", "", info.get("title") or vid).strip()
        download_name = f"{title or vid}.mp4"

        set_state(status="done", progress=1.0, file_path=str(chosen),
                  download_name=download_name,
                  detail=f"Ready ({chosen.stat().st_size / (1024 * 1024):.1f} MB)")
    except yt_dlp.utils.DownloadError as exc:
        set_state(status="error", error=friendly_error(exc))
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures clearly
        set_state(status="error", error=friendly_error(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", ffmpeg_available=check_ffmpeg() is not None)


@app.route("/api/download", methods=["POST"])
def start_download():
    purge_expired()

    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()

    if not url:
        return jsonify(error="Please paste a YouTube URL first."), 400
    video_id = extract_video_id(url)
    if video_id is None:
        return jsonify(
            error="That doesn't look like a valid YouTube URL. "
                  "Expected a link like https://www.youtube.com/watch?v=… , "
                  "/shorts/… , or https://youtu.be/…"
        ), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "starting",
            "progress": 0.0,
            "detail": "Fetching video info…",
            "error": None,
            "file_path": None,
            "download_name": None,
            "created_at": time.time(),
            "video_id": video_id,
        }

    thread = threading.Thread(
        target=run_download, args=(job_id, url, check_ffmpeg()), daemon=True
    )
    thread.start()
    return jsonify(job_id=job_id)


@app.route("/api/progress/<job_id>")
def progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            abort(404)
        state = dict(job)
    state.pop("file_path", None)  # never leak server paths
    return jsonify(state)


@app.route("/api/file/<job_id>")
def file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job.get("status") != "done":
            abort(404)
        file_path, download_name = job["file_path"], job["download_name"]
    try:
        # The <video> preview uses ?inline=1; the Save button (default)
        # requests the file as an attachment so the browser downloads it.
        as_attachment = "inline" not in request.args
        return send_file(file_path, as_attachment=as_attachment,
                         download_name=download_name, mimetype="video/mp4",
                         conditional=True)
    except FileNotFoundError:
        abort(410)  # cached file expired — tell the client to re-download


if __name__ == "__main__":
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=8000, debug=False)
