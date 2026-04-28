"""MCP server entry point.

Exposes a single tool, `read_article`, that fetches a WeChat Official Account
article and returns:

  [text_block, image_block, image_block, ..., text_block, image_block, ...]

where:
  - text_block #1 has metadata + the full article markdown
  - image_blocks immediately following are the article's PNG/JPG images
    (GIFs filtered out, capped at 10 per article)
  - subsequent text+image groups are per-video (info + keyframes)

On failure the response is a single text block whose body starts with "Error:".
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from .fetcher import FetchError, fetch_html
from .media import (
    download_images,
    extract_video_keyframes,
    filter_image_urls,
)
from .parser import Article, parse_article

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("weixin-articles")

_MAX_IMAGES = 10
_MAX_VIDEOS = 3


def _render_text_block(article: Article) -> str:
    """Format article metadata + body markdown as a single text block."""
    from .markdown import html_to_markdown

    body_md = html_to_markdown(article.body_html) if article.body_html else "(empty body)"
    return (
        f"# {article.title}\n"
        f"Account: {article.account}\n"
        f"Published: {article.publish_time or '(unknown)'}\n"
        f"Cover: {article.cover_url or '(none)'}\n"
        f"Videos: {len(article.videos)}\n\n"
        f"---\n\n"
        f"{body_md}"
    )


async def _read_article_impl(url: str) -> list[Any]:
    """Core implementation, kept decoupled from the MCP decorator for testability."""
    if not url.startswith("https://mp.weixin.qq.com/s/"):
        return ["Error: URL must start with https://mp.weixin.qq.com/s/"]

    logger.info(f"fetching: {url}")
    try:
        html = await fetch_html(url)
    except FetchError as e:
        return [f"Error: fetch failed: {e}"]

    article = parse_article(html)
    logger.info(
        f"parsed: title={article.title!r} "
        f"images={len(article.image_urls)} videos={len(article.videos)}"
    )

    blocks: list[Any] = [_render_text_block(article)]

    image_urls = filter_image_urls(article.image_urls, limit=_MAX_IMAGES)
    if image_urls:
        logger.info(f"downloading {len(image_urls)} image(s)")
        images = await download_images(image_urls)
        blocks.extend(images)

    for video in article.videos[:_MAX_VIDEOS]:
        logger.info(f"processing video: kind={video.kind}")
        info, frames = await extract_video_keyframes(video)
        blocks.append(info)
        blocks.extend(frames)

    return blocks


# output_schema=None: this tool returns a heterogeneous list of MCP content
# blocks (text + image), not structured data. Without this, FastMCP would try
# to validate the return against an inferred schema and fail.
@mcp.tool(output_schema=None)
async def read_article(url: str) -> list:
    """
    Read a WeChat Official Account article (mp.weixin.qq.com/s/...) and
    return its content as a multimodal block list:

    - First text block: title, account, publish time, cover URL, video count,
      and the full article body in Markdown.
    - Image blocks: the article's PNG/JPG images (GIFs filtered out, capped at
      10) returned as native image content so the LLM can see them directly.
    - Per-video groups: a text marker `video kind=... vid=... keyframes=N`
      followed by N evenly-spaced keyframe images (capped at 3 videos).

    On failure the response is a single text block starting with "Error:".

    Args:
        url: A WeChat article URL, format: https://mp.weixin.qq.com/s/xxx
    """
    return await _read_article_impl(url)


def main() -> None:
    """Entry point for `weixin-articles-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
