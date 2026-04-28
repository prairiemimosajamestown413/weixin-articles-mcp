"""Media handling: image download and video keyframe extraction.

Images: filter to PNG/JPG (skip GIFs which are usually decorative), download
in parallel, return as FastMCP `Image` content blocks.

Videos: download with yt-dlp (Tencent Video supported), then ffmpeg-extract
N evenly-spaced keyframes. Returns one `text` block (per-video metadata) plus
N `Image` blocks (the keyframes).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp.utilities.types import Image

from .parser import VideoRef

logger = logging.getLogger(__name__)

# Image filtering / download
_RASTER_KEEP = {"png", "jpg", "jpeg"}
_IMAGE_DOWNLOAD_TIMEOUT = 10.0
_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # skip images larger than this

# Video processing
_VIDEO_KEYFRAMES = 8
_VIDEO_DOWNLOAD_TIMEOUT_S = 120
_FFMPEG_TIMEOUT_S = 60


def is_raster_url(url: str) -> bool:
    """True if URL points to a PNG/JPG (not GIF)."""
    parsed = urlparse(url)
    fmt = parse_qs(parsed.query).get("wx_fmt", [""])[0].lower()
    if fmt:
        return fmt in _RASTER_KEEP
    path = parsed.path.lower()
    if "gif" in path:
        return False
    return any(ext in path for ext in _RASTER_KEEP)


def filter_image_urls(urls: list[str], limit: int) -> list[str]:
    """Keep only raster URLs, preserve order, dedupe, cap at limit."""
    seen: set[str] = set()
    kept: list[str] = []
    for u in urls:
        if u in seen or not is_raster_url(u):
            continue
        seen.add(u)
        kept.append(u)
        if len(kept) >= limit:
            break
    return kept


async def _download_one_image(client: httpx.AsyncClient, url: str) -> Image | None:
    try:
        resp = await client.get(url, timeout=_IMAGE_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"image download failed [{url}]: {e}")
        return None
    data = resp.content
    if len(data) > _IMAGE_MAX_BYTES:
        logger.warning(f"image too large, skipped [{url}]: {len(data)}B")
        return None
    ctype = resp.headers.get("content-type", "").lower()
    if "png" in ctype:
        fmt = "png"
    elif "jpeg" in ctype or "jpg" in ctype:
        fmt = "jpeg"
    else:
        fmt = "jpeg" if "jpg" in url.lower() or "jpeg" in url.lower() else "png"
    return Image(data=data, format=fmt)


async def download_images(urls: list[str]) -> list[Image]:
    """Download image URLs concurrently, preserving order, skipping failures."""
    if not urls:
        return []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_download_one_image(client, u) for u in urls), return_exceptions=False
        )
    return [img for img in results if img is not None]


async def extract_video_keyframes(
    video: VideoRef, *, n_frames: int = _VIDEO_KEYFRAMES
) -> tuple[str, list[Image]]:
    """Download a single video and extract evenly-spaced keyframes.

    Returns (info_text, keyframe_images). Returns ("error: ...", []) if
    yt-dlp / ffmpeg are unavailable or the download fails.
    """
    if not shutil.which("yt-dlp"):
        return ("video skipped: yt-dlp not installed", [])
    if not shutil.which("ffmpeg"):
        return ("video skipped: ffmpeg not installed", [])

    download_url = _video_download_url(video)
    if download_url is None:
        return (f"video skipped: kind={video.kind} not yet supported", [])

    with tempfile.TemporaryDirectory(prefix="wxa-vid-") as tmp:
        tmp_path = Path(tmp)
        try:
            video_path = await _yt_dlp_download(download_url, tmp_path)
        except Exception as e:
            logger.warning(f"yt-dlp failed [{download_url}]: {e}")
            return (f"video download failed: {e}", [])

        try:
            frames = await _ffmpeg_keyframes(video_path, tmp_path, n_frames)
        except Exception as e:
            logger.warning(f"ffmpeg failed [{video_path}]: {e}")
            return (f"video keyframe extraction failed: {e}", [])

    info = f"video kind={video.kind} vid={video.vid or video.mpvid or '?'} keyframes={len(frames)}"
    return (info, frames)


def _video_download_url(video: VideoRef) -> str | None:
    """Build a downloadable URL for the video, or None if unsupported."""
    if video.kind == "tencent" and video.vid:
        return f"https://v.qq.com/x/page/{video.vid}.html"
    # WeChat Channels videos (wxv_*) require a logged-in token; out of scope
    return None


async def _yt_dlp_download(url: str, out_dir: Path) -> Path:
    out_template = str(out_dir / "video.%(ext)s")
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "-q",
        "--no-warnings",
        "-f",
        "best[height<=480]/best",  # keep file small; keyframes don't need 4K
        "-o",
        out_template,
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_VIDEO_DOWNLOAD_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"yt-dlp timeout after {_VIDEO_DOWNLOAD_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip()[:200])

    # yt-dlp picks ext based on format; find whatever it produced
    candidates = list(out_dir.glob("video.*"))
    if not candidates:
        raise RuntimeError("yt-dlp produced no output file")
    return candidates[0]


async def _ffmpeg_keyframes(video_path: Path, out_dir: Path, n: int) -> list[Image]:
    """Extract n evenly-spaced JPEG frames from video_path."""
    duration = await _ffprobe_duration(video_path)
    if duration <= 0 or n <= 0:
        return []

    # Sample at 5%, 5%+(95%/n), ..., up to 95% — skip credits/intro
    start, end = duration * 0.05, duration * 0.95
    step = (end - start) / max(n - 1, 1) if n > 1 else 0
    timestamps = [start + i * step for i in range(n)]

    images: list[Image] = []
    for i, t in enumerate(timestamps):
        frame_path = out_dir / f"frame_{i:02d}.jpg"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.2f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "4",
            str(frame_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            continue
        if proc.returncode == 0 and frame_path.exists():
            images.append(Image(data=frame_path.read_bytes(), format="jpeg"))
    return images


async def _ffprobe_duration(video_path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0
