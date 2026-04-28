"""HTTP fetcher for WeChat article pages.

WeChat's mp.weixin.qq.com is friendly to non-logged-in GET requests as long as
a real browser User-Agent is sent. No cookie or token required.
"""

from __future__ import annotations

import httpx

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 20.0


class FetchError(RuntimeError):
    """Fetch failed (network, HTTP non-2xx, or anti-bot block)."""


async def fetch_html(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """GET a WeChat article URL and return raw HTML."""
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
