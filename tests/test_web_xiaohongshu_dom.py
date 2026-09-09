"""可控离线 DOM 浏览器测试：默认跳过，显式环境开关才运行。

全部文档由 route.fulfill 提供，context offline 且所有其它请求 abort。
使用 fixture.invalid 并仅在测试中扩充来源白名单；绝不访问真实平台。
"""
from base64 import b64encode
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from opinion_capture import web_xiaohongshu as adapter
from opinion_capture.web_results_xiaohongshu import SCROLL_JS, SNAPSHOT_JS


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / ".local/web_xiaohongshu/goal-20260908-complete"


def fixture_html(count=8, *, end=True, suggestions=False, broken=False, nested_scroll=False, extra=""):
    """构建明确标注的人工页面；不是保留平台 DOM 的删改或重绘。"""
    from PIL import Image
    stream = BytesIO()
    Image.new("RGB", (240, 160), "#98c8b0").save(stream, format="PNG")
    src = "data:image/png;base64," + b64encode(stream.getvalue()).decode()
    cards = []
    for i in range(1, count+1):
        identity = f"{i:024x}"
        image_src = "https://denied.invalid/missing.png" if broken and i == 1 else src
        cards.append(f'<section class="note-item" data-note-id="{identity}">'
            f'<a class="cover" href="https://www.xiaohongshu.com/explore/{identity}">'
            f'<img src="{image_src}" width="240" height="160"></a>'
            f'<div class="title">离线夹具笔记 {i}</div><p>不同高度的原生卡片内容 {i}</p>'
            + ('<span class="ad-tag">推广</span>' if i == 2 else '') + '</section>')
    notes = ''.join(cards)
    panel = '<div class="sug-container" id="sug"><div class="sug-item">零跑汽车</div><div class="sug-item">零跑车型</div><div class="sug-item">零跑价格</div></div>' if suggestions else ''
    end_html = '<div class="no-more">没有更多了</div>' if end else ''
    scroll_style = 'height:440px;overflow:auto;' if nested_scroll else ''
    return f'''<!doctype html><html><meta charset="utf-8"><title>离线人工 DOM 夹具</title>
    <style>body{{margin:0;font:18px 'Microsoft YaHei',sans-serif;background:#f8fafb;}}
    header{{position:sticky;top:0;height:110px;background:white;z-index:10;padding:8px 20px;box-sizing:border-box;}}
    h1{{font-size:20px;margin:0 0 8px;color:#9d1424;}}input{{font:18px 'Microsoft YaHei';height:28px;width:320px;}}
    .search-tabs{{margin-top:7px;}}.active{{color:#ca2233;border-bottom:2px solid;}}
    .feeds-container{{display:grid;grid-template-columns:repeat(3,240px);gap:20px;padding:20px;width:760px;}}
    .note-item{{width:240px;background:white;border:1px solid #cbd6d9;box-sizing:border-box;overflow:hidden;}}
    .note-item:nth-child(3n+1){{min-height:300px;}}.note-item img{{display:block;width:100%;}}
    .title,p{{margin:8px;font-size:16px;}}.ad-tag{{background:#f6d7b6;margin:8px;}}
    .no-more{{padding:24px;text-align:center;width:740px;}}.sug-container{{position:fixed;left:20px;top:77px;background:white;
      width:330px;z-index:30;border:1px solid #777;}}.sug-item{{height:42px;padding-left:12px;line-height:42px;}}
    </style><body><header><h1>OFFLINE FIXTURE · 人工 DOM，非平台验收</h1>
    <input id="search-input" value="零跑" aria-controls="sug"><div class="search-tabs"><span class="active">综合</span>　用户　视频</div></header>
    {panel}<main style="{scroll_style}"><div class="feeds-container">{notes}</div>{end_html}</main>{extra}
    <script>document.querySelector('input').focus();</script></body></html>'''


@unittest.skipUnless(os.environ.get("OPINION_XHS_OFFLINE_BROWSER_TESTS") == "1", "仅显式离线 DOM 浏览器验收时运行")
class OfflineDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = adapter._local_playwright()
        cls.playwright = cls.manager.__enter__()
        browsers = adapter._browser_paths(cls.playwright)
        if not browsers:
            cls.manager.__exit__(None, None, None)
            raise unittest.SkipTest("本机浏览器未安装")
        cls.browser = cls.playwright.chromium.launch(executable_path=str(browsers[0]), headless=True)
        cls.evidence_dir = EVIDENCE_ROOT / ("offline-dom-" + uuid4().hex[:10])
        cls.evidence_dir.mkdir(parents=True)
        cls.network_log = []

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.manager.__exit__(None, None, None)
        (cls.evidence_dir/"network-audit.json").write_text(json.dumps(dict(
            evidence_source="offline_dom_fixture", real_platform_requests=0, external_requests_forwarded=0,
            requests=cls.network_log), ensure_ascii=False, indent=2), encoding="utf-8")

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 960, "height": 650}, offline=True, service_workers="block")
        self.page = self.context.new_page()
        self.html = fixture_html()

        def route(request_route):
            url = request_route.request.url
            self.network_log.append(dict(url=url, action="fulfill_fixture" if url.startswith("https://fixture.invalid/") else "abort"))
            if url.startswith("https://fixture.invalid/"):
                request_route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self.html)
            else:
                request_route.abort()
        self.context.route("**/*", route)

    def tearDown(self):
        self.context.close()

    def load(self, html=None):
        self.html = html or fixture_html()
        self.page.goto("https://fixture.invalid/search_result?keyword=%E9%9B%B6%E8%B7%91", wait_until="load")

    def collect(self, name, top_n=5, kind="results"):
        run = self.evidence_dir / name
        run.mkdir()
        (run/"fixture-input.html").write_text(self.html, encoding="utf-8")
        result = adapter._base_result()
        request = adapter.validate_request(dict(keyword="零跑", capture_kind=kind, top_n=top_n, output_dir=str(run)))
        result.update(keyword="零跑", capture_kind=kind, requested_top_n=top_n)
        evidence = dict(frames=[], http_status=200, evidence_source="offline_dom_fixture", rank_policy=adapter.RANK_POLICY_DETAIL)
        with patch.object(adapter, "SOURCE_HOSTS", {"fixture.invalid"}):
            adapter._collect(self.page, request, run, result, evidence, adapter.Budget(25))
        return result, evidence

    def test_top_n_raw_frames_primary_and_manifest(self):
        self.load()
        result, evidence = self.collect("top5", top_n=5)
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["rank_verified"])
        self.assertGreater(len(result["source_artifact_paths"]), 1)
        self.assertTrue(result["items"][1]["promoted"])
        self.assertEqual(len([i for i in result["items"] if i["counted"]]), 5)
        self.assertEqual(result["rank_policy"], "independent_content_cards")
        self.assertEqual(len([p for p in result["artifact_paths"] if Path(p).name == 'primary.png']), 1)
        from PIL import Image, ImageChops
        with Image.open(result["primary_artifact_path"]) as primary:
            for frame, placement in zip(evidence["frames"], evidence["primary_layout"]):
                self.assertEqual(hashlib.sha256(Path(frame["path"]).read_bytes()).hexdigest(), frame["sha256"])
                with Image.open(frame["path"]) as source:
                    region = primary.crop((0, placement["y"], placement["width"], placement["y"]+placement["height"]))
                    self.assertIsNone(ImageChops.difference(region.convert('RGB'), source.convert('RGB')).getbbox())
        self.assertEqual(result["source_url"].split('/')[2], "fixture.invalid")

    def test_explicit_short_last_page(self):
        self.load(fixture_html(count=2))
        result, evidence = self.collect("insufficient", top_n=5)
        self.assertEqual((result["status"], result["outcome"]), ("success", "insufficient_results"), result)
        self.assertEqual(result["observed_count"], 2)
        self.assertEqual(len(evidence["frames"]), 2)

    def test_empty_page_with_explicit_end(self):
        self.load(fixture_html(count=0))
        result, _ = self.collect("empty", top_n=5)
        self.assertEqual((result["status"], result["observed_count"]), ("success", 0), result)

    def test_stall_is_not_proof_of_end(self):
        self.load(fixture_html(count=2, end=False))
        result, _ = self.collect("stalled", top_n=5)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["outcome"], "incomplete")

    def test_missing_image_stays_partial(self):
        self.load(fixture_html(count=2, broken=True))
        result, _ = self.collect("broken-image", top_n=2)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["rank_verified"])

    def test_native_scroll_container(self):
        self.load(fixture_html(nested_scroll=True))
        self.assertEqual(self.page.evaluate(SNAPSHOT_JS)["scroll"]["host"], "feed_ancestor")
        result, _ = self.collect("nested-scroll", top_n=6)
        self.assertEqual(result["status"], "success", result)

    def test_scroll_appends_new_notes_until_top_n(self):
        script = '''<script>let added=false;window.addEventListener('scroll',()=>{
          if(added)return;added=true;const root=document.querySelector('.feeds-container');
          for(let i=7;i<=9;i++){const node=root.firstElementChild.cloneNode(true),id=i.toString(16).padStart(24,'0');
            node.setAttribute('data-note-id',id);node.querySelector('a').href='https://www.xiaohongshu.com/explore/'+id;
            node.querySelector('.title').textContent='滚动新增离线笔记 '+i;root.appendChild(node);}
          const end=document.createElement('div');end.className='no-more';end.textContent='没有更多了';root.parentElement.appendChild(end);
        });</script>'''
        self.load(fixture_html(count=6, end=False, extra=script))
        result, evidence = self.collect("scroll-load-more", top_n=8)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(len(evidence["frames"][0]["before"]["results"]), 6)
        self.assertEqual(result["observed_count"], 9)

    def test_delayed_image_is_waited_for_before_success(self):
        html = fixture_html(count=2)
        html = html.replace('</body>', '''<script>const image=document.querySelector('.cover img'),src=image.src;
          image.src='https://denied.invalid/pending.png';setTimeout(()=>image.src=src,650);</script></body>''')
        self.load(html)
        result, evidence = self.collect("delayed-image", top_n=2)
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(evidence["frames"][0]["before"]["results"][0]["images_loaded"])

    def test_hidden_ancestor_is_not_counted(self):
        self.load(fixture_html(count=2, extra='<style>main{opacity:0}</style>'))
        self.assertEqual(self.page.evaluate(SNAPSHOT_JS)["results"], [])

    def test_suggestions_ignore_broken_background_note_image(self):
        self.load(fixture_html(count=2, suggestions=True, broken=True))
        result, _ = self.collect("suggestions-background-image", kind="suggestions")
        self.assertEqual(result["status"], "success", result)

    def test_suggestions_complete_and_exact_boundary(self):
        self.load(fixture_html(count=0, suggestions=True, end=False))
        result, _ = self.collect("suggestions", kind="suggestions")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual([i["text"] for i in result["items"]], ["零跑汽车", "零跑车型", "零跑价格"])
        self.assertFalse(result["rank_verified"])

    def test_suggestions_clipped_not_complete(self):
        self.load(fixture_html(count=0, suggestions=True, extra='<style>.sug-container{height:60px;overflow:auto}</style>'))
        result, _ = self.collect("suggestions-clipped", kind="suggestions")
        self.assertEqual(result["status"], "partial")

    def test_hidden_suggestions_or_two_panels_not_success(self):
        for extra in ('<style>.sug-container{display:none}</style>', '<div class="suggestion-container">另一个面板</div>'):
            self.load(fixture_html(count=0, suggestions=True, extra=extra))
            self.assertFalse(self.page.evaluate(SNAPSHOT_JS)["suggestion_panel_complete"])

    def test_history_and_accounts_not_counted(self):
        extra = '<div class="search-history">零跑历史记录</div><div class="related-search"><section class="note-item"><a href="https://www.xiaohongshu.com/explore/000000000000000000000099">相关推荐</a></section></div><div class="user-list"><section class="note-item">账号聚合</section></div>'
        self.load(fixture_html(count=2, extra=extra))
        state = self.page.evaluate(SNAPSHOT_JS)
        self.assertEqual(len(state["results"]), 2)
        self.assertEqual(state["suggestions"], [])

    def test_note_mentions_verification_is_not_captcha_wall(self):
        html = fixture_html(count=2).replace('离线夹具笔记 1', '安全验证功能讨论')
        self.load(html)
        self.assertIsNone(adapter._block(self.page.evaluate(SNAPSHOT_JS)))

    def test_malformed_card_and_conflicting_note_links(self):
        html = fixture_html(count=2).replace('data-note-id="000000000000000000000001"', 'data-note-id="000000000000000000000099"')
        self.load(html)
        state = self.page.evaluate(SNAPSHOT_JS)
        self.assertEqual(state["results"][0]["identity_error"], "conflicting_note_identity")

    def test_occluding_overlay_prevents_coverage(self):
        self.load(fixture_html(count=2, extra='<div style="position:fixed;inset:120px 0 0;background:#ddd;z-index:99">夹具遮挡层</div>'))
        state = self.page.evaluate(SNAPSHOT_JS)
        self.assertIsNone(state["results"][0]["coverage"])


if __name__ == "__main__":
    unittest.main()
