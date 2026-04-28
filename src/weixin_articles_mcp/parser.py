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
# Tencent Video iframe: data-src=//v.qq.com/iframe/preview.html?vid=XXXX
_TENCENT_VID_RE = re.compile(r'(?:vid|videoid)=([A-Za-z0-9_-]+)')


@dataclass
class VideoRef:
    """A video embedded in the article body.

    `kind` is one of:
      - "tencent": Tencent Video (v.qq.com), `vid` set, downloadable via yt-dlp
      - "wxv":     WeChat Channels native video (wxv_*), `mpvid` set, requires
                   special handling (out of scope for v0.1)
      - "unknown": unrecognized iframe, only `raw_src` set
    """

    kind: str
    vid: str | None = None
    mpvid: str | None = None
    raw_src: str = ""
    cover: str | None = None


@dataclass
class Article:
    title: str
    account: str
    publish_time: str  # ISO-8601 string, or "" if unknown
    cover_url: str
    body_html: str  # raw inner HTML of #js_content, for downstream MD conversion
    image_urls: list[str] = field(default_factory=list)
    videos: list[VideoRef] = field(default_factory=list)


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
    videos = _extract_videos(body)
    return Article(
        title=title,
        account=account,
        publish_time=publish_time,
        cover_url=cover,
        body_html=str(body),
        image_urls=images,
        videos=videos,
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


def _extract_videos(body: Tag) -> list[VideoRef]:
    """Identify embedded videos. Two main forms:

    1. Tencent Video iframe (most common):
       <iframe class="video_iframe" data-src="//v.qq.com/iframe/preview.html?vid=XXX">
    2. WeChat Channels native video (Shipinhao):
       <iframe class="video_iframe" data-mpvid="wxv_XXX">
    """
    videos: list[VideoRef] = []
    for iframe in body.find_all("iframe"):
        data_src = iframe.get("data-src") or iframe.get("src") or ""
        mpvid = iframe.get("data-mpvid", "")
        cover = iframe.get("data-cover") or None

        if mpvid and mpvid.startswith("wxv_"):
            videos.append(VideoRef(kind="wxv", mpvid=mpvid, raw_src=data_src, cover=cover))
            continue

        if "v.qq.com" in data_src or "iframe/preview.html" in data_src:
            m = _TENCENT_VID_RE.search(data_src)
            if m:
                videos.append(VideoRef(kind="tencent", vid=m.group(1), raw_src=data_src, cover=cover))
                continue

        if data_src:
            videos.append(VideoRef(kind="unknown", raw_src=data_src, cover=cover))
    return videos
