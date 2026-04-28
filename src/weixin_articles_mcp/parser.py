"""Parse WeChat article HTML into structured data.

The DOM landmarks below have been stable across WeChat's mp.weixin.qq.com
template for years:
  - #activity-name        article title
  - #js_name               account display name
  - #js_content           article body container
  - meta[property=og:image] cover image
  - var ct = "<unix>"     publish timestamp embedded in inline JS
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

_PUBLISH_TS_RE = re.compile(r'var\s+ct\s*=\s*["\'](\d+)["\']')
_BIZ_RE = re.compile(r'var\s+biz\s*=\s*["\']([^"\']+)["\']')
_MID_RE = re.compile(r'var\s+mid\s*=\s*["\']?(\d+)')
_IDX_RE = re.compile(r'var\s+idx\s*=\s*["\']?(\d+)')
# Tencent Video iframe: data-src=//v.qq.com/iframe/preview.html?vid=XXXX
_TENCENT_VID_RE = re.compile(r'(?:vid|videoid)=([A-Za-z0-9_-]+)')

# WeChat-native video URLs are emitted in inline JS as:
#   url: JsDecode('http://mpvideo.qpic.cn/<vid>.f<fmt>.mp4?dis_k=...&auth_key=...')
# Each video typically appears at 4 quality levels (f10002 超清, f10004 流畅,
# f10102, f10104), all sharing the same <vid> path segment.
_MP4_URL_RE = re.compile(
    r"""url:\s*JsDecode\(['"]([^'"]+\.f\d+\.mp4[^'"]*)['"]\)"""
)
_MP4_VIDEO_ID_RE = re.compile(r"/([0-9a-zA-Z]+)\.f\d+\.mp4")


@dataclass
class VideoRef:
    """A video embedded in the article body.

    `kind` is one of:
      - "tencent":  Tencent Video iframe (v.qq.com), `vid` set, downloadable
                    via yt-dlp.
      - "wxv":      WeChat Official Account native video, embedded as
                    <iframe data-mpvid="wxv_<digits>">. `raw_src` is a direct
                    mp4 URL extracted from inline JS (mp_video_trans_info).
      - "wxv-snap": Modern WeChat Channels (视频号) embed via the
                    <mp-common-videosnap> custom element. The mp4 stream
                    itself is locked behind WeChat's finder protocol and
                    cannot be retrieved without a logged-in WeChat client
                    (verified via source-level review of 8+ open-source
                    downloaders, all of which require MITM proxy + WeChat
                    PC client). What we CAN retrieve via the public
                    batch_get_video_snap API: high-resolution cover image,
                    duration, dimensions, full description, like count,
                    publisher verification — populated by
                    media.enrich_videosnap_metadata().
      - "unknown":  Unrecognized iframe, only `raw_src` set.
    """

    kind: str
    vid: str | None = None
    mpvid: str | None = None
    raw_src: str = ""  # for wxv: mp4 URL; for wxv-snap: cover image URL
    cover: str | None = None
    description: str = ""
    nickname: str = ""
    # Filled in by media.enrich_videosnap_metadata() for kind=wxv-snap
    username: str = ""  # finder username, needed for the API call
    duration_s: int = 0
    width: int = 0
    height: int = 0
    like_count: str = ""
    verified: bool = False


@dataclass
class Article:
    title: str
    account: str
    publish_time: str  # ISO-8601 string, or "" if unknown
    cover_url: str
    body_html: str  # raw inner HTML of #js_content, for downstream MD conversion
    image_urls: list[str] = field(default_factory=list)
    videos: list[VideoRef] = field(default_factory=list)
    # Article-level identifiers, used to call the batch_get_video_snap API
    # when we want enriched metadata for embedded WeChat Channels videos.
    biz: str = ""
    mid: str = ""
    idx: str = ""


def parse_article(html: str) -> Article:
    soup = BeautifulSoup(html, "lxml")

    title = _text_of(soup.select_one("#activity-name"))
    account = _text_of(soup.select_one("#js_name"))
    cover = _attr_of(soup.select_one('meta[property="og:image"]'), "content")
    publish_time = _extract_publish_time(html)

    body = soup.select_one("#js_content")
    if body is None:
        # No body container -> empty article
        return Article(
            title=title,
            account=account,
            publish_time=publish_time,
            cover_url=cover,
            body_html="",
        )

    images = _extract_images(body)
    # Videos: pass the full HTML because native-video mp4 URLs live in inline
    # JS (mp_video_trans_info), outside the #js_content tree.
    videos = _extract_videos(html, body)
    biz_m = _BIZ_RE.search(html)
    mid_m = _MID_RE.search(html)
    idx_m = _IDX_RE.search(html)
    return Article(
        title=title,
        account=account,
        publish_time=publish_time,
        cover_url=cover,
        body_html=str(body),
        image_urls=images,
        videos=videos,
        biz=biz_m.group(1) if biz_m else "",
        mid=mid_m.group(1) if mid_m else "",
        idx=idx_m.group(1) if idx_m else "",
    )


def _text_of(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if tag is not None else ""


def _attr_of(tag: Tag | None, attr: str) -> str:
    return tag.get(attr, "") if tag is not None else ""


def _extract_publish_time(html: str) -> str:
    m = _PUBLISH_TS_RE.search(html)
    if not m:
        return ""
    try:
        ts = int(m.group(1))
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return ""


def _extract_images(body: Tag) -> list[str]:
    """Extract image URLs from body, preserving order, de-duplicated.

    WeChat lazy-loads images via `data-src` attribute; `src` is usually empty
    or a placeholder.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for img in body.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src or src in seen:
            continue
        seen.add(src)
        urls.append(src)
    return urls


def _extract_videos(html: str, body: Tag) -> list[VideoRef]:
    """Identify embedded videos. Three forms encountered in the wild:

    1. Modern WeChat Channels (视频号) embeds via custom element:
         <mp-common-videosnap data-url="https://findermp.video.qq.com/.../*.mp4"
                              data-desc="..." data-nickname="...">
       The data-url is a direct mp4 CDN link, downloadable with plain httpx.

    2. WeChat Official Account native videos via iframe:
         <iframe class="video_iframe" data-mpvid="wxv_<digits>"
                 data-cover="..." data-src="...readtemplate?...vid=wxv_...">
       The iframe DOES NOT carry the mp4 URL. The URL lives in inline JS as
       `mp_video_trans_info: [{ url: JsDecode('http://mpvideo.qpic.cn/<vid>.f10004.mp4...') }]`.
       We extract those URLs from the full HTML and pair them with iframes
       by document order.

    3. Tencent Video (v.qq.com) iframe:
         <iframe data-src="//v.qq.com/iframe/preview.html?vid=XXX">
       Downloadable with yt-dlp.
    """
    videos: list[VideoRef] = []

    # (1) Modern WeChat Channels embeds. The `data-url` here is the LOW-RES
    # cover thumbnail, not the video — the actual mp4 lives behind WeChat's
    # finder protocol and isn't reachable from public web. Downstream code
    # (media.enrich_videosnap_metadata) calls the batch_get_video_snap API
    # to upgrade `raw_src` to a hi-res cover and fill in duration/dimensions.
    for snap in body.find_all("mp-common-videosnap"):
        data_url = snap.get("data-url") or ""
        videos.append(
            VideoRef(
                kind="wxv-snap",
                mpvid=snap.get("data-id", "") or None,
                raw_src=data_url,  # placeholder cover; upgraded by enrichment
                cover=snap.get("data-headimgurl") or None,
                description=snap.get("data-desc", ""),
                nickname=snap.get("data-nickname", ""),
                username=snap.get("data-username", ""),
            )
        )

    # (2) Native videos: pair each <iframe data-mpvid="wxv_*"> with an mp4 URL
    # extracted from the inline JS, in order of appearance.
    native_iframes = [
        ifr
        for ifr in body.find_all("iframe")
        if (ifr.get("data-mpvid") or "").startswith("wxv_")
    ]
    native_mp4_urls = _extract_native_mp4_urls(html)
    for i, ifr in enumerate(native_iframes):
        videos.append(
            VideoRef(
                kind="wxv",
                mpvid=ifr.get("data-mpvid", ""),
                raw_src=native_mp4_urls[i] if i < len(native_mp4_urls) else "",
                cover=ifr.get("data-cover") or None,
            )
        )

    # (3) Other iframes (Tencent Video / unknown). Skip ones already handled
    # in (2).
    for iframe in body.find_all("iframe"):
        if (iframe.get("data-mpvid") or "").startswith("wxv_"):
            continue
        data_src = iframe.get("data-src") or iframe.get("src") or ""
        cover = iframe.get("data-cover") or None
        if "v.qq.com" in data_src or "iframe/preview.html" in data_src:
            m = _TENCENT_VID_RE.search(data_src)
            if m:
                videos.append(
                    VideoRef(
                        kind="tencent", vid=m.group(1), raw_src=data_src, cover=cover
                    )
                )
                continue
        if data_src:
            videos.append(VideoRef(kind="unknown", raw_src=data_src, cover=cover))

    return videos


def _extract_native_mp4_urls(html: str) -> list[str]:
    """Extract one mp4 URL per WeChat-native video from inline JS.

    Each video emits 4 quality levels in mp_video_trans_info, all with the same
    `/<video_id>.f<format>.mp4` path. We dedupe by `<video_id>` (preserving
    first-occurrence order) and prefer the f10004 ('流畅', smallest, ~1-2MB)
    quality because keyframes don't need higher resolution. If f10004 isn't
    available for a given video_id, we fall back to whichever quality came
    first in the document.
    """
    # Map: video_id -> (preferred_url, fallback_url)
    by_vid: dict[str, tuple[str, str]] = {}
    order: list[str] = []  # preserve appearance order of new video_ids

    for m in _MP4_URL_RE.finditer(html):
        raw_url = m.group(1)
        # Decode hex-escaped & html-entity-escaped ampersands embedded in URL
        url = (
            raw_url.replace(r"\x26amp;", "&")
            .replace(r"\x26", "&")
            .replace("&amp;", "&")
        )
        vid_match = _MP4_VIDEO_ID_RE.search(url)
        if not vid_match:
            continue
        video_id = vid_match.group(1)
        if video_id not in by_vid:
            by_vid[video_id] = ("", url)  # (preferred, fallback)
            order.append(video_id)
        # If this is the f10004 quality, mark it as preferred
        if ".f10004.mp4" in url:
            preferred, fallback = by_vid[video_id]
            by_vid[video_id] = (url, fallback)

    return [by_vid[vid][0] or by_vid[vid][1] for vid in order]
