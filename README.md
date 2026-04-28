# weixin-articles-mcp

> MCP server for reading **WeChat (微信) Official Account articles**, with native multimodal output — images and video keyframes returned as content blocks, not URLs.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub stars](https://img.shields.io/github/stars/jj-cheng25/weixin-articles-mcp?style=social)](https://github.com/jj-cheng25/weixin-articles-mcp)

## Why this exists

Other tools that read WeChat articles for LLMs return **a list of image URLs** — your LLM has to click through to actually see them, costing extra round-trips and context.

This server returns **the images themselves**. And the **video keyframes**. Your LLM sees what you see, in one shot.

| Tool | Article text | Images | Videos |
|---|---|---|---|
| `WebFetch` (built-in) | ✅ (often blocked by anti-bot) | ❌ URLs only | ❌ |
| Existing WeChat MCPs / Skills | ✅ | ❌ URLs only | ❌ |
| **weixin-articles-mcp** | ✅ | ✅ **Native image blocks** | ✅ **Keyframes as image blocks** |

## Features

- 📰 **Reliable WeChat scraping** — pure Python `httpx` GET, no Rust binary or headless browser required
- 🖼️ **Native image content** — PNG/JPG returned as MCP `Image` blocks, GIFs filtered, capped at 10 per article
- 🎬 **Video keyframe extraction** — Tencent Video embeds downloaded with `yt-dlp`, 8 evenly-spaced frames extracted with `ffmpeg`, returned as image blocks
- 🕒 **Publish time recovery** — extracts `var ct` Unix timestamp that other parsers miss
- 🪶 **Minimal install** — `pip install` + optional `ffmpeg` for video; no Chromium, no Rust

## Install

```bash
# Core (article + images)
pip install weixin-articles-mcp

# With video keyframe support
pip install "weixin-articles-mcp[video]"
brew install ffmpeg   # or apt install ffmpeg on Linux
```

## Configure

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "weixin-articles": {
      "command": "weixin-articles-mcp"
    }
  }
}
```

### Cursor / Cline

Same JSON, drop into the MCP server config of your client.

## Usage

Once configured, just paste a WeChat article URL into your conversation:

> Read https://mp.weixin.qq.com/s/cexkyzQBRDG3uIF6g5cEbQ

Your LLM will receive:
- Article metadata (title, account, publish time, cover URL)
- Full article body in Markdown
- All inline PNG/JPG images as native image content blocks
- For each video, 8 keyframe images

## Tool reference

### `read_article(url: str) -> list[content_block]`

Returns a list of MCP content blocks:

- `[0]` — text block: metadata + article body markdown
- `[1..N]` — image blocks: article images (max 10)
- For each video (max 3): one text marker + 8 keyframe image blocks

On failure, returns a single text block starting with `Error:`.

## Roadmap

- [x] WeChat article fetching with anti-bot handling
- [x] Native image content blocks
- [x] Tencent Video keyframe extraction
- [ ] WeChat Channels (视频号) video support
- [ ] ASR subtitles via faster-whisper
- [ ] Full-text search across read articles
- [ ] Account subscription / new-article notifications

## Architecture

```
src/weixin_articles_mcp/
├── server.py     # FastMCP entrypoint, tool registration
├── fetcher.py    # httpx GET with browser UA
├── parser.py     # WeChat DOM extraction (BeautifulSoup + lxml)
├── markdown.py   # HTML → Markdown (markdownify subclass)
└── media.py      # Image download + video download/keyframe extraction
```

## Contributing

PRs welcome. Particularly looking for help on:
- WeChat Channels (视频号) URL handling
- Resilience to template variants from less common publishers
- More test fixtures (different article styles)

Open an issue: https://github.com/jj-cheng25/weixin-articles-mcp/issues

## License

MIT — see [LICENSE](LICENSE).
