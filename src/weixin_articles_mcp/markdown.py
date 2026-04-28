"""HTML to Markdown conversion, with WeChat-specific tweaks.

Built on `markdownify`, but with overrides:
  - <img> uses `data-src` (lazy-load) rather than empty `src`
  - <iframe> video embeds get rewritten to a `[video: kind=... vid=...]` marker
    so the LLM-facing text knows where each video sits in the flow
"""

from __future__ import annotations

import re

from markdownify import MarkdownConverter

from .parser import VideoRef, _TENCENT_VID_RE


class _WeixinConverter(MarkdownConverter):
    """Subclass of markdownify converter with WeChat tweaks."""

    def convert_img(self, el, text, parent_tags):  # noqa: ARG002 - signature fixed
        src = el.get("data-src") or el.get("src") or ""
        alt = el.get("alt") or ""
        if not src:
            return ""
        return f"![{alt}]({src})"

    def convert_iframe(self, el, text, parent_tags):  # noqa: ARG002
        data_src = el.get("data-src") or el.get("src") or ""
        mpvid = el.get("data-mpvid", "")
        if mpvid and mpvid.startswith("wxv_"):
            return f"\n\n[video kind=wxv mpvid={mpvid}]\n\n"
        if "v.qq.com" in data_src or "iframe/preview.html" in data_src:
            m = _TENCENT_VID_RE.search(data_src)
            if m:
                return f"\n\n[video kind=tencent vid={m.group(1)}]\n\n"
        if data_src:
            return f"\n\n[video src={data_src}]\n\n"
        return ""


_CONVERTER_OPTS = {
    "heading_style": "ATX",
    "bullets": "-",
    "strong_em_symbol": "*",
    "code_language_callback": lambda _: "",
    "escape_asterisks": False,
    "escape_underscores": False,
}


def html_to_markdown(html: str) -> str:
    """Convert WeChat article body HTML to Markdown."""
    md = _WeixinConverter(**_CONVERTER_OPTS).convert(html)
    # Collapse 3+ blank lines that markdownify sometimes emits
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()
