"""Media handling: image download and video keyframe extraction.

Images: filter to PNG/JPG (skip GIFs which are usually decorative), download
in parallel, return as FastMCP `Image` content blocks.

Videos:
  - WeChat-native (kind="wxv"): direct httpx mp4 download, then ffmpeg-extract
    N evenly-spaced keyframes.
  - Tencent Video (kind="tencent"): yt-dlp download, same keyframe pipeline.
  - WeChat Channels (kind="wxv-snap"): mp4 stream is locked behind the
    finder protocol; we instead call the public batch_get_video_snap API to
    enrich each VideoRef with hi-res cover, duration, dimensions, like count
    and verification status, then return the cover image as a single visual.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp.utilities.types import Image

from .parser import Article, VideoRef

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

    Returns (info_text, frame_images). info_text always includes the
    video's kind and identifier; on success it reports the frame count, on
    failure it explains why. If the video carries a description/nickname
    (WeChat Channels embeds), those are appended so the LLM gets semantic
    context even if keyframe extraction is impossible.
    """
    # WeChat Channels (视频号) embeds expose only a cover image to logged-out
    # users — the actual mp4 stream requires a finder API call with auth that
    # we don't have. The data-url field is actually the cover JPEG, not the
    # video. Downloading it gives the LLM something visual; combined with the
    # description+nickname text, this covers most of the video's information.
    if video.kind == "wxv-snap":
        cover = await _download_cover_image(video.raw_src)
        status = "cover only (mp4 requires WeChat login)"
        return (_video_info_text(video, status), [cover] if cover else [])

    if not shutil.which("ffmpeg"):
        return (_video_info_text(video, "ffmpeg not installed"), [])

    # WeChat-native (wxv) videos have direct mp4 CDN URLs we extracted from
    # the article HTML — httpx them. Tencent Video iframes need yt-dlp because
    # the URL we construct is the player page, not mp4.
    use_httpx = video.kind == "wxv"
    if not use_httpx and not shutil.which("yt-dlp"):
        return (_video_info_text(video, "yt-dlp not installed"), [])

    download_url = _video_download_url(video)
    if download_url is None:
        return (_video_info_text(video, "kind not supported"), [])

    with tempfile.TemporaryDirectory(prefix="wxa-vid-") as tmp:
        tmp_path = Path(tmp)
        try:
            if use_httpx:
                video_path = await _httpx_download_video(download_url, tmp_path)
            else:
                video_path = await _yt_dlp_download(download_url, tmp_path)
        except Exception as e:
            logger.warning(f"download failed [{download_url}]: {e}")
            return (_video_info_text(video, f"download failed: {e}"), [])

        try:
            frames = await _ffmpeg_keyframes(video_path, tmp_path, n_frames)
        except Exception as e:
            logger.warning(f"ffmpeg failed [{video_path}]: {e}")
            return (_video_info_text(video, f"keyframe extraction failed: {e}"), [])

    return (_video_info_text(video, f"keyframes={len(frames)}"), frames)


def _video_info_text(video: VideoRef, status: str) -> str:
    """Compose the per-video text marker. Includes whatever metadata is
    available so the LLM has semantic context even when video frames can't
    be extracted."""
    ident = video.vid or video.mpvid or "?"

    # Inline metadata that fits on the marker line: duration, dimensions
    inline_meta = []
    if video.duration_s:
        inline_meta.append(f"duration={video.duration_s}s")
    if video.width and video.height:
        inline_meta.append(f"{video.width}x{video.height}")
    inline = (" " + " ".join(inline_meta)) if inline_meta else ""

    parts = [f"[video kind={video.kind} id={ident}{inline} {status}]"]
    if video.nickname:
        nick = video.nickname
        if video.verified:
            nick += " (verified)"
        parts.append(f"by {nick}")
    if video.like_count:
        parts.append(f"likes: {video.like_count}")
    if video.description:
        parts.append(f"description: {video.description.strip()}")
    return "\n".join(parts)


def _video_download_url(video: VideoRef) -> str | None:
    """Build a downloadable URL for the video, or None if unsupported."""
    if video.kind == "tencent" and video.vid:
        return f"https://v.qq.com/x/page/{video.vid}.html"
    if video.kind in ("wxv-snap", "wxv") and video.raw_src:
        return video.raw_src  # already a direct mp4 CDN URL
    return None


async def enrich_videosnap_metadata(article: Article) -> None:
    """Mutate `article.videos` in place: for each kind="wxv-snap" video, call
    WeChat's public batch_get_video_snap API to fill in duration, dimensions,
    like count, hi-res cover URL and verification status.

    No cookie or session is required — the API accepts empty auth fields.
    Failures are non-fatal: if the API doesn't respond as expected we just
    leave the existing data-attribute fields in place. One API call covers
    all videosnap videos in the article.
    """
    snaps = [v for v in article.videos if v.kind == "wxv-snap"]
    if not snaps:
        return
    if not (article.biz and article.mid and article.idx):
        logger.warning("missing biz/mid/idx, skipping videosnap enrichment")
        return

    api = (
        "https://mp.weixin.qq.com/mp/appmsg_video_snap"
        "?action=batch_get_video_snap&uin=&key=&pass_ticket=&wxtoken=777"
        f"&__biz={article.biz}&appmsg_token=&x5=0"
    )
    body_parts = [
        f"__biz={article.biz}",
        f"mid={article.mid}",
        f"idx={article.idx}",
        "uin=",
        "key=",
        "pass_ticket=",
    ]
    for i, v in enumerate(snaps):
        body_parts.append(f"exportid_{i}={v.mpvid or ''}")
        body_parts.append(f"username_{i}={v.username}")
    body_parts.append(f"video_snap_num={len(snaps)}")
    body = "&".join(body_parts)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": "https://mp.weixin.qq.com/",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(api, content=body, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        logger.warning(f"videosnap enrichment API call failed: {e}")
        return

    if payload.get("base_resp", {}).get("ret") != 0:
        logger.warning(
            f"videosnap enrichment returned non-zero ret: "
            f"{payload.get('base_resp')}"
        )
        return

    info_list = payload.get("video_snap_info", [])
    # Index returned entries by export_id so we don't rely on order.
    by_export = {entry.get("export_id", ""): entry for entry in info_list}
    for v in snaps:
        entry = by_export.get(v.mpvid or "")
        if not entry:
            continue
        # Upgrade cover from low-res placeholder to hi-res
        full_cover = entry.get("feed_full_cover_url") or entry.get("feed_cover_url")
        if full_cover:
            v.raw_src = full_cover
        v.duration_s = int(entry.get("feed_video_play_len_s") or 0)
        v.width = int(entry.get("feed_width") or 0)
        v.height = int(entry.get("feed_height") or 0)
        v.like_count = str(entry.get("feed_like_num") or "")
        v.verified = bool(entry.get("auth_info", {}).get("auth_icon_url"))
        # Prefer the API's nickname/desc if our inline fallback was empty
        if not v.nickname and entry.get("nickname"):
            v.nickname = entry["nickname"]
        if not v.description and entry.get("feed_desc"):
            v.description = entry["feed_desc"]


async def _download_cover_image(url: str) -> Image | None:
    """Download a video cover image (JPEG/PNG). Used as fallback content for
    WeChat Channels videos whose mp4 we can't access."""
    if not url:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_IMAGE_DOWNLOAD_TIMEOUT, headers=headers
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning(f"cover image download failed [{url}]: {e}")
        return None
    data = resp.content
    if len(data) > _IMAGE_MAX_BYTES:
        logger.warning(f"cover image too large, skipped: {len(data)}B")
        return None
    ctype = resp.headers.get("content-type", "").lower()
    fmt = "png" if "png" in ctype else "jpeg"
    return Image(data=data, format=fmt)


async def _httpx_download_video(url: str, out_dir: Path) -> Path:
    """Direct mp4 download via httpx. Used for WeChat-native and Channels
    videos whose URL is already a direct CDN link."""
    out_path = out_dir / "video.mp4"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
    }
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_VIDEO_DOWNLOAD_TIMEOUT_S, headers=headers
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
        if not data:
            raise RuntimeError("empty response body")
        out_path.write_bytes(data)
    return out_path


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
