"""Unit tests for parser and media URL filtering.

Network-dependent end-to-end tests live in test_e2e.py (not in this file).
"""

from weixin_articles_mcp.media import filter_image_urls, is_raster_url
from weixin_articles_mcp.parser import parse_article


_PNG = "https://mmbiz.qpic.cn/sz_mmbiz_png/x/640?wx_fmt=png&from=appmsg"
_JPG = "https://mmbiz.qpic.cn/mmbiz_jpg/x/640?wx_fmt=jpeg&from=appmsg"
_GIF = "https://mmbiz.qpic.cn/sz_mmbiz_gif/x/640?wx_fmt=gif&from=appmsg"


def test_is_raster_url():
    assert is_raster_url(_PNG)
    assert is_raster_url(_JPG)
    assert not is_raster_url(_GIF)
    assert is_raster_url("https://example.com/foo.png")
    assert not is_raster_url("https://example.com/foo.gif")


def test_filter_image_urls_dedupe_and_cap():
    urls = [_GIF, _PNG, _JPG, _PNG, _GIF]
    kept = filter_image_urls(urls, limit=10)
    assert kept == [_PNG, _JPG]


def test_filter_image_urls_respects_limit():
    urls = [f"https://x.com/{i}.png?wx_fmt=png" for i in range(20)]
    kept = filter_image_urls(urls, limit=5)
    assert len(kept) == 5


def test_parse_article_title_and_publish_time():
    html = """
    <html><body>
    <h1 id="activity-name"> Hello World </h1>
    <a id="js_name">My Account</a>
    <meta property="og:image" content="http://x.com/cover.jpg"/>
    <script>var ct = "1700000000";</script>
    <div id="js_content"><p>body</p></div>
    </body></html>
    """
    art = parse_article(html)
    assert art.title == "Hello World"
    assert art.account == "My Account"
    assert art.cover_url == "http://x.com/cover.jpg"
    assert art.publish_time.startswith("2023-")  # ts=1700000000 → 2023-11-14


def test_parse_article_video_extraction():
    html = """
    <html><body>
    <div id="js_content">
      <iframe class="video_iframe" data-src="//v.qq.com/iframe/preview.html?vid=g0042abc"></iframe>
      <iframe class="video_iframe" data-mpvid="wxv_1234567890"></iframe>
      <iframe data-src="https://other.com/video"></iframe>
    </div>
    </body></html>
    """
    art = parse_article(html)
    assert len(art.videos) == 3
    assert art.videos[0].kind == "tencent"
    assert art.videos[0].vid == "g0042abc"
    assert art.videos[1].kind == "wxv"
    assert art.videos[1].mpvid == "wxv_1234567890"
    assert art.videos[2].kind == "unknown"


def test_parse_article_image_extraction_uses_data_src():
    html = """
    <html><body>
    <div id="js_content">
      <img src="" data-src="https://x.com/1.png"/>
      <img data-src="https://x.com/2.png"/>
      <img src="https://x.com/3.png"/>
    </div>
    </body></html>
    """
    art = parse_article(html)
    assert art.image_urls == [
        "https://x.com/1.png",
        "https://x.com/2.png",
        "https://x.com/3.png",
    ]


def test_parse_article_handles_missing_body():
    html = "<html><body><h1 id='activity-name'>T</h1></body></html>"
    art = parse_article(html)
    assert art.title == "T"
    assert art.body_html == ""
    assert art.image_urls == []
    assert art.videos == []
