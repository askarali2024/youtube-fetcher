"""YouTube Downloader — a lightweight Streamlit app.

Paste a YouTube video or Shorts URL, click Download, and save the video
as an MP4. Uses yt-dlp for extraction/download and FFmpeg (if available)
to merge high-quality video + audio streams.
"""

import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import streamlit as st
import yt_dlp

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "yt_shorts_downloader"
FILE_MAX_AGE_SECONDS = 60 * 30  # purge cached files older than 30 min
# Files above this size skip the in-browser preview (too heavy to load);
# the download button still works.
PREVIEW_MAX_MB = 150

st.set_page_config(page_title="YouTube Downloader", page_icon="🎬", layout="centered")


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


def cleanup_old_files() -> None:
    """Remove cached downloads older than FILE_MAX_AGE_SECONDS."""
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for path in DOWNLOAD_DIR.iterdir():
            if path.is_file() and now - path.stat().st_mtime > FILE_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
    except OSError:
        pass  # cleanup is best-effort only


def download_video(url: str, video_id: str, ffmpeg_path: str | None) -> Path:
    """Download the video and return the path to the final MP4 file.

    Raises yt_dlp.utils.DownloadError (or generic exceptions) on failure.
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    progress = st.empty()
    status_text = st.empty()

    def progress_hook(data: dict) -> None:
        if data["status"] == "downloading":
            percent = data.get("_percent_str", "?").strip()
            speed = data.get("_speed_str", "?").strip()
            eta = data.get("_eta_str", "?").strip()
            progress.progress(
                _safe_percent(data),
                text=f"Downloading… {percent} at {speed} (ETA {eta})",
            )
        elif data["status"] == "finished":
            progress.progress(1.0, text="Download complete — processing…")

    ydl_opts: dict = {
        "outtmpl": str(DOWNLOAD_DIR / f"{video_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "restrictfilenames": True,
    }

    if ffmpeg_path:
        # Best video + best audio, merged into a single MP4 via FFmpeg.
        ydl_opts.update(
            {
                "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
                "merge_output_format": "mp4",
                "ffmpeg_location": ffmpeg_path,
            }
        )
    else:
        # No FFmpeg: fall back to the best single-file (progressive) MP4
        # so downloads still work, just possibly at a lower quality.
        ydl_opts["format"] = "b[ext=mp4]/b"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    finally:
        progress.empty()
        status_text.empty()

    video_id = info.get("id", video_id)

    # yt-dlp may remux/merge into a different extension, so locate the
    # actual output file rather than trusting the predicted filename.
    candidates = sorted(DOWNLOAD_DIR.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime)
    mp4s = [p for p in candidates if p.suffix == ".mp4"]
    chosen = mp4s[-1] if mp4s else (candidates[-1] if candidates else None)

    if chosen is None or not chosen.exists() or chosen.stat().st_size == 0:
        raise FileNotFoundError("Download finished but no output file was produced.")
    return chosen


def _safe_percent(data: dict) -> float:
    """Extract a 0.0–1.0 progress fraction from a yt-dlp progress dict."""
    if data.get("total_bytes") is not None:
        return min(data["downloaded_bytes"] / data["total_bytes"], 1.0)
    if data.get("total_bytes_estimate") is not None:
        return min(data["downloaded_bytes"] / data["total_bytes_estimate"], 1.0)
    return 0.0


def friendly_error(exc: Exception) -> str:
    """Translate common yt-dlp failures into clear user-facing messages."""
    message = str(exc).lower()
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


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

cleanup_old_files()

st.title("🎬 YouTube Downloader")
st.caption("Paste a YouTube video or Shorts URL, hit Download, and save the video as an MP4.")

ffmpeg_path = check_ffmpeg()
if ffmpeg_path is None:
    st.warning(
        "**FFmpeg was not found** (neither on your system nor via the bundled "
        "`imageio-ffmpeg` package). Some videos only offer separate video and "
        "audio streams, which cannot be merged without FFmpeg — those downloads "
        "will fail. Install FFmpeg (see the README) or run "
        "`pip install imageio-ffmpeg` to fix this.",
        icon="⚠️",
    )

with st.form("download_form"):
    url = st.text_input(
        "YouTube URL",
        placeholder="https://www.youtube.com/watch?v=… or /shorts/…",
    )
    submitted = st.form_submit_button("Download", type="primary", use_container_width=True)

if submitted:
    if not url.strip():
        st.error("Please paste a YouTube URL first.")
    elif (video_id := extract_video_id(url)) is None:
        st.error(
            "That doesn't look like a valid YouTube URL.\n\n"
            "Expected a link like `https://www.youtube.com/watch?v=…` , "
            "`https://www.youtube.com/shorts/…` , or `https://youtu.be/…`."
        )
    else:
        try:
            with st.spinner("Fetching video info…"):
                file_path = download_video(url, video_id, ffmpeg_path)
            st.session_state["downloaded_file"] = file_path
            st.session_state["downloaded_at"] = time.time()
        except yt_dlp.utils.DownloadError as exc:
            st.error(friendly_error(exc))
        except FileNotFoundError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 — surface unexpected failures clearly
            st.error(friendly_error(exc))

# Offer the completed download (persists across reruns until a new download).
file_path = st.session_state.get("downloaded_file")
if file_path and Path(file_path).exists():
    file_bytes = Path(file_path).read_bytes()
    size_mb = len(file_bytes) / (1024 * 1024)

    st.success(f"Ready! ({size_mb:.1f} MB)")
    if size_mb <= PREVIEW_MAX_MB:
        st.video(file_bytes)
    else:
        st.info(
            "This file is too large to preview in the browser — "
            "use the download button below to save it."
        )

    st.download_button(
        "⬇️ Save to your device",
        data=file_bytes,
        file_name=Path(file_path).name,
        mime="video/mp4",
        type="primary",
        use_container_width=True,
    )
    if st.button("Clear", help="Remove the cached file and start over."):
        Path(file_path).unlink(missing_ok=True)
        for key in ("downloaded_file", "downloaded_at"):
            st.session_state.pop(key, None)
        st.rerun()
else:
    st.session_state.pop("downloaded_file", None)
