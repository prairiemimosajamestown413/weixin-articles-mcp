# weixin-articles-mcp

> MCP server for reading **WeChat (微信) Official Account articles**, with native multimodal output — images and video keyframes returned as content blocks, not URLs.
>
> **For personal/research use.** This tool reads only publicly accessible article URLs and does not bypass any authentication or anti-bot measures. See [Disclaimer](#disclaimer) before using.

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
- 🎬 **Video handling for all three embed types**:
  - **WeChat Official Account native videos** (`<iframe data-mpvid="wxv_*">`): mp4 extracted from inline JS, 8 evenly-spaced keyframes via ffmpeg
  - **Tencent Video** (`v.qq.com` iframes): yt-dlp + ffmpeg keyframes
  - **WeChat Channels** (视频号, `<mp-common-videosnap>`): full metadata via the public `batch_get_video_snap` API (duration, dimensions, hi-res cover, full description, like count, publisher verification) + cover image. *mp4 stream is locked behind WeChat's finder protocol — see [Why no Channels mp4?](#why-no-channels-mp4) below*
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
- `[1..N]` — image blocks: article images (max 10, GIFs filtered)
- For each video (max 3):
  - **WeChat-native or Tencent**: one text marker + 8 keyframe image blocks
  - **WeChat Channels**: one text marker (with duration, dimensions, like count, publisher, description) + 1 hi-res cover image block

On failure, returns a single text block starting with `Error:`.

## Roadmap

- [x] WeChat article fetching with anti-bot handling
- [x] Native image content blocks
- [x] WeChat Official Account native video keyframe extraction
- [x] Tencent Video keyframe extraction
- [x] WeChat Channels (视频号) metadata enrichment via public API
- [ ] ASR subtitles via faster-whisper (for native + Tencent videos)
- [ ] Full-text search across read articles
- [ ] Account subscription / new-article notifications

### Why no Channels mp4?

Short answer: WeChat Channels (视频号) videos in articles intentionally don't expose a downloadable mp4 stream to public web access. The mp4 lives inside WeChat's finder protocol, which requires (a) a logged-in WeChat client session, (b) finder-specific encryption (the first 128KB of the mp4 is XOR-encrypted with a fixed key), and (c) intercepting the stream from the WeChat PC client at network level.

Every open-source WeChat Channels downloader in the wild — [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download), [qiye45/wechatVideoDownload](https://github.com/qiye45/wechatVideoDownload), [putyy/res-downloader](https://github.com/putyy/res-downloader), [KingsleyYau/WeChatChannelsDownloader](https://github.com/KingsleyYau/WeChatChannelsDownloader) and others — solves this with a **MITM HTTPS proxy + WeChat PC client + root CA installation**. That model is fundamentally incompatible with how an MCP server runs (no client, no user interaction, no admin install).

What we do instead: call WeChat's public `batch_get_video_snap` API (no cookie or session required) to give your LLM the next-best thing — high-resolution cover image, full description, duration, dimensions, like count, and publisher verification. For most use cases (reading and summarizing articles), this is enough to convey the video's substance.

#### Pairing with wx_channels_download for full Channels mp4

If your workflow really needs the mp4 file (archiving, transcoding, frame-perfect inspection), pair this MCP with [wx_channels_download](https://github.com/ltaoo/wx_channels_download), the most active open-source Channels downloader:

| Tool | Role |
|---|---|
| **weixin-articles-mcp** (this) | LLM reads article body + images + native/Tencent video keyframes + Channels metadata (cover, duration, description) |
| **wx_channels_download** | You download the actual Channels mp4 by playing it in WeChat PC client (MITM proxy intercepts the stream) |

Suggested flow:

1. Ask your LLM to read the article via this MCP. It surfaces which Channels videos are embedded, their durations, descriptions, and covers.
2. Based on that summary, decide whether you want the raw mp4.
3. If yes, open the article in WeChat PC client with `wx_channels_download` running, hit play, click its injected download button.

This division keeps the MCP itself zero-side-effects (only reads public URLs, no MITM, no client) while still giving you a path to the full mp4 when you actually need it.

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

## Disclaimer

This tool is provided for **personal, educational, and research use only**.

What this tool does:
- Reads publicly accessible WeChat article URLs (`mp.weixin.qq.com/s/...`) using a standard browser User-Agent — the same content any user with a web browser can view
- Calls only public WeChat API endpoints that accept empty authentication fields (i.e. designed by WeChat to be reachable without login)
- Enforces a default 1-second minimum interval between requests to prevent the tool from being repurposed as a high-volume crawler

What this tool **does not** do:
- Use cookies, login sessions, or any form of user credential
- Bypass any technical protection, anti-bot measure, or encrypted stream (e.g. WeChat Channels mp4 is intentionally **not** supported — see [Why no Channels mp4?](#why-no-channels-mp4))
- Decrypt, reverse-engineer, or circumvent WeChat's protocol-level protections
- Store, cache, or redistribute fetched content beyond the immediate response

User responsibilities:
- **Respect WeChat's Terms of Service** when using this tool. Personal/research use of publicly accessible articles is generally aligned with how the content is intended to be consumed; high-volume scraping or commercial redistribution likely is not.
- **Respect copyright** of fetched content. Article content remains the property of its original authors and publishers; this tool only fetches and forwards it to your LLM for inline processing.
- **Do not flood mp.weixin.qq.com** — keep usage at human reading rates. The default rate limit is set conservatively, but you can tighten it further by setting `WEIXIN_FETCH_INTERVAL_S=2.0` (or higher) in your environment.

The authors and contributors of this project disclaim all liability arising from misuse. By using this software you accept full responsibility for ensuring your usage complies with applicable laws and the terms of service of the services it connects to.

## License

MIT — see [LICENSE](LICENSE).
