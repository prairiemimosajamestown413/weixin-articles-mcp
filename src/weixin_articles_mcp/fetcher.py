"""HTTP fetcher for WeChat article pages.

WeChat's mp.weixin.qq.com is friendly to non-logged-in GET requests as long as
a real browser User-Agent is sent. No cookie or token required.

This module enforces a minimum inter-request delay so the tool can't easily
be repurposed as a high-throughput crawler. The default (1.0s) is invisible
to interactive use but slows aggressive automation. Override via the
WEIXIN_FETCH_INTERVAL_S environment variable (minimum 0.5s).
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 20.0


def _read_min_interval() -> float:
    raw = os.environ.get("WEIXIN_FETCH_INTERVAL_S", "1.0")
    try:
        v = float(raw)
    except ValueError:
        return 1.0
    return max(v, 0.5)


_MIN_INTERVAL_S = _read_min_interval()
_last_fetch_ts: float = 0.0
_fetch_lock = asyncio.Lock()


class FetchError(RuntimeError):
    """Fetch failed (network, HTTP non-2xx, or anti-bot block)."""


async def fetch_html(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """GET a WeChat article URL and return raw HTML.

    Rate-limited: enforces a minimum interval between successive requests
    (default 1.0s) to discourage use as a high-volume crawler.
    """
    global _last_fetch_ts
    async with _fetch_lock:
        elapsed = time.monotonic() - _last_fetch_ts
        if elapsed < _MIN_INTERVAL_S:
            await asyncio.sleep(_MIN_INTERVAL_S - elapsed)
        _last_fetch_ts = time.monotonic()

    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise FetchError(f"network error: {e}") from e

    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")

    text = resp.text
    if "请输入验证码" in text or "环境异常" in text:
        raise FetchError("anti-bot challenge detected")
    return text
