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


def test_parse_article_video_extraction_iframe_kinds():
    # WeChat-native (wxv_) iframes are emitted first in the videos list,
    # then Tencent Video iframes, then unknown iframes. The order matches
    # the pairing logic for inline-JS mp4 URLs (which we don't include here).
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
    # wxv_ iframes are processed first
    assert art.videos[0].kind == "wxv"
    assert art.videos[0].mpvid == "wxv_1234567890"
    # then tencent
    assert art.videos[1].kind == "tencent"
    assert art.videos[1].vid == "g0042abc"
    # unknown last
    assert art.videos[2].kind == "unknown"


def test_parse_article_videosnap_extraction():
    """Modern WeChat Channels embeds via <mp-common-videosnap>. The data-url
    is actually a cover image (mp4 requires WeChat login), but description
    and nickname carry the video's semantic content."""
    html = """
    <html><body>
    <div id="js_content">
      <mp-common-videosnap
        data-pluginname="mpvideosnap"
        data-url="https://findermp.video.qq.com/cover.jpg?picformat=200"
        data-id="export/UzABC"
        data-nickname="My Channel"
        data-desc="Talking about model design"
        data-headimgurl="https://wx.qlogo.cn/avatar.jpg"></mp-common-videosnap>
    </div>
    </body></html>
    """
    art = parse_article(html)
    assert len(art.videos) == 1
    v = art.videos[0]
    assert v.kind == "wxv-snap"
    assert v.mpvid == "export/UzABC"
    assert v.raw_src.startswith("https://findermp.video.qq.com/")
    assert v.nickname == "My Channel"
    assert v.description == "Talking about model design"
    assert v.cover == "https://wx.qlogo.cn/avatar.jpg"


def test_parse_article_native_video_pairs_iframe_with_inline_mp4():
    """Native videos: each <iframe data-mpvid="wxv_*"> in #js_content gets
    paired (by document order) with an mp4 URL from inline JS."""
    html = """
    <html><head>
    <script>
      var stuff = {
        mp_video_trans_info: [
          { format_id: '10002', url: JsDecode('http://mpvideo.qpic.cn/AAAAA.f10002.mp4?auth=x') },
          { format_id: '10004', url: JsDecode('http://mpvideo.qpic.cn/AAAAA.f10004.mp4?auth=y') },
          { format_id: '10004', url: JsDecode('http://mpvideo.qpic.cn/BBBBB.f10004.mp4?auth=z') },
        ],
      };
    </script>
    </head><body>
    <div id="js_content">
      <iframe class="video_iframe" data-mpvid="wxv_aaa" data-cover="cover_a.jpg"></iframe>
      <iframe class="video_iframe" data-mpvid="wxv_bbb"></iframe>
    </div>
    </body></html>
    """
    art = parse_article(html)
    assert len(art.videos) == 2
    # First wxv_ pairs with the f10004 quality of the AAAAA video
    assert art.videos[0].kind == "wxv"
    assert art.videos[0].mpvid == "wxv_aaa"
    assert ".f10004.mp4" in art.videos[0].raw_src
    assert "AAAAA" in art.videos[0].raw_src
    # Second wxv_ pairs with BBBBB
    assert art.videos[1].mpvid == "wxv_bbb"
    assert "BBBBB" in art.videos[1].raw_src


def test_parse_article_native_video_falls_back_to_other_quality():
    """If f10004 isn't present for a video_id, fall back to whatever quality
    appeared (preserving the URL's hex/entity decoding)."""
    html = """
    <html><head>
    <script>
      mp_video_trans_info: [
        { url: JsDecode('http://mpvideo.qpic.cn/CCCC.f10002.mp4?a=1\\x26amp;b=2') },
      ];
    </script>
    </head><body>
    <div id="js_content">
      <iframe class="video_iframe" data-mpvid="wxv_only_super"></iframe>
    </div>
    </body></html>
    """
    art = parse_article(html)
    assert len(art.videos) == 1
    v = art.videos[0]
    assert v.kind == "wxv"
    assert ".f10002.mp4" in v.raw_src
    # Hex-escaped &amp; should be decoded to plain &
    assert "&b=2" in v.raw_src
    assert "\\x26" not in v.raw_src


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


def test_parse_article_extracts_biz_mid_idx():
    """biz/mid/idx come from inline JS variables and are needed to call the
    batch_get_video_snap API for WeChat Channels enrichment."""
    html = """
    <html><head>
    <script>
      var biz = "MzkyOTkxODg3NA==";
      var mid = "2247489133";
      var idx = "1";
    </script>
    </head><body><div id="js_content"><p>x</p></div></body></html>
    """
    art = parse_article(html)
    assert art.biz == "MzkyOTkxODg3NA=="
    assert art.mid == "2247489133"
    assert art.idx == "1"


def test_parse_article_videosnap_extracts_username_for_api_call():
    """The data-username attribute is required to call batch_get_video_snap
    later — without it, enrichment can't construct a valid request."""
    html = """
    <html><body><div id="js_content">
      <mp-common-videosnap
        data-url="https://findermp.video.qq.com/cover.jpg"
        data-id="export/UzAAA"
        data-username="v2_xxx@finder"
        data-nickname="N"
        data-desc="D"></mp-common-videosnap>
    </div></body></html>
    """
    art = parse_article(html)
    assert art.videos[0].username == "v2_xxx@finder"
