# weixin-articles-mcp container image.
#
# Designed to satisfy the Glama MCP Registry's introspection check: the
# container must launch the server, which then responds to MCP `initialize`
# and `tools/list` requests over stdio.
#
# Includes ffmpeg for video keyframe extraction (Tencent Video and
# WeChat-native videos). yt-dlp is installed via the [video] extras.
#
# Usage:
#   docker build -t weixin-articles-mcp .
#   docker run -i --rm weixin-articles-mcp

FROM python:3.12-slim

# ffmpeg is required for video keyframe extraction. Use --no-install-recommends
# to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package metadata first so Docker can cache the dependency install layer
# when only source files change.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the package with video extras (yt-dlp).
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[video]"

# stdio MCP server: stdin/stdout speaks JSON-RPC, banner goes to stderr.
ENTRYPOINT ["weixin-articles-mcp"]
