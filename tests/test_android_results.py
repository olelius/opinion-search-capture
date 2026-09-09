"""离线结构回归与合成像素算法测试；合成帧不代表真实APP多屏验收。"""
from copy import deepcopy
import hashlib
from pathlib import Path
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET

from PIL import Image

from opinion_capture.android_results import EvidenceError, RankLedger, parse_page, render_long, match_frames


ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / ".local/android/verified/20260908-220409-7c86b654f5/hierarchy.xml"


class SavedXMLTests(unittest.TestCase):
    """本机已保存原生XML回归，缺证据环境显式skip而非改为合成样本。"""

    @unittest.skipUnless(REAL.is_file(), "本机真实B站XML未随仓库分发")
    def test_real_bili_independent_promoted_and_aggregate(self):
        xml = REAL.read_text(encoding="utf-8")
        page = parse_page(xml, "bilibili", "results", "零跑")
        self.assertTrue(page["recognized"])
        self.assertTrue(page["keyword_verified"])
        self.assertTrue(page["scope_verified"])
        self.assertEqual(len(page["items"]), 4)
        self.assertIn("朱江明×罗永浩", page["items"][0]["title"])
        self.assertTrue(page["items"][1]["promoted"])
        self.assertEqual(page["items"][1]["author"], "HappyWen_")
        self.assertIn("AR-HUD", page["items"][2]["title"])
        self.assertEqual(len(page["excluded"]), 2)
        self.assertTrue(all(i["reason"] == "account_aggregate" for i in page["excluded"]))
        self.assertNotIn("把黑珍珠", repr(page["items"]))
        ledger = RankLedger(3)
        ledger.add(dict(page=page, png_path=str(REAL.with_name("screen.png")), xml_path=str(REAL)))
        self.assertTrue(ledger.target_ready())
        self.assertEqual([i["rank"] for i in ledger.items[:3]], [1, 2, 3])
        self.assertEqual(ledger.items[2]["content_bounds"][3], 1657)

    def test_actual_suggestions_scoped_away_from_hidden_results(self):
        path = ROOT / ".local/android/verified/20260908-220404-33d62fa192/hierarchy.xml"
        if not path.exists():
            self.skipTest("本机B站下拉XML缺失")
        page = parse_page(path.read_text(encoding="utf-8"), "bilibili", "suggestions", "零跑")
        self.assertEqual(len(page["items"]), 10)
        self.assertTrue(page["complete"])
        self.assertEqual(page["items"][0]["title"], "零跑a10")
        self.assertNotIn("IC实验室", repr(page["items"]))

    def test_actual_xhs_accounts_and_products_are_not_words(self):
        path = ROOT / ".local/android/final/20260908-220314-c1b692fb1c/hierarchy.xml"
        if not path.exists():
            self.skipTest("本机小红书下拉XML缺失")
        page = parse_page(path.read_text(encoding="utf-8"), "xiaohongshu", "suggestions", "零跑")
        self.assertEqual(len(page["items"]), 12)
        self.assertTrue(page["complete"])
        self.assertEqual(page["end_marker"]["text"], "反馈")
        self.assertEqual([i["reason"] for i in page["excluded"]], ["account_suggestion", "product_suggestion"])

    @unittest.skipUnless(REAL.is_file(), "本机真实B站XML未随仓库分发")
    def test_wrong_keyword_and_unknown_module_rejected(self):
        xml = REAL.read_text(encoding="utf-8")
        page = parse_page(xml, "bilibili", "results", "另一个词")
        with self.assertRaisesRegex(EvidenceError, "scope_or_keyword"):
            RankLedger(3).add(dict(page=page))
        page = parse_page(xml, "bilibili", "results", "零跑")
        page["unknown"].append(dict(reason="unknown", bounds=[0, 500, 1080, 600]))
        ledger = RankLedger(3)
        ledger.add(dict(page=page, png_path="raw.png", xml_path="raw.xml"))
        self.assertFalse(ledger.target_ready())

    def test_real_scroll_collapsed_tabs_and_dynamic_exclusion(self):
        base = ROOT / ".local/android/goal-live-stage2/20260908-222204-594c2864e4/source"
        if not base.exists():
            self.skipTest("本机实际滚动帧未随仓库分发")
        ledger = RankLedger(3)
        for i in range(2):
            xml = base / f"frame-{i:03d}-before.xml"
            page = parse_page(xml.read_text(encoding="utf-8"), "bilibili", "results", "零跑")
            ledger.add(dict(page=page, png_path=str(base / f"frame-{i:03d}.png"), xml_path=str(xml)))
        self.assertTrue(ledger.target_ready())
        self.assertEqual(ledger.links[0]["shift"], 407)
        self.assertEqual(ledger.links[0]["mean_error"], 0)
        self.assertEqual(ledger.frames[-1]["page"]["scope_verified"], None)
        self.assertIn("non_video_dynamic", [e["reason"] for e in ledger.excluded])

    def test_saved_logged_in_xhs_visual_order_not_recycled_index(self):
        base = ROOT / ".local/android/goal-xhs-inspect-223152"
        if not base.exists():
            self.skipTest("本机已登录小红书证据未随仓库分发")
        ledger = RankLedger(4)
        for i in range(2):
            xml = base / f"{i:03d}.xml"
            page = parse_page(xml.read_text(encoding="utf-8"), "xiaohongshu", "results", "零跑")
            ledger.add(dict(page=page, png_path=str(base / f"{i:03d}.png"), xml_path=str(xml)))
        self.assertTrue(ledger.frames[0]["page"]["start_verified"])
        self.assertEqual([i["author"] for i in ledger.items], ["引力加速团", "车探MAX", "车境纪事", "唠车日常"])
        self.assertEqual(ledger.links[0]["shift"], 396)

    def test_saved_xhs_ad_retained_and_scrolled_start_rejected(self):
        base = ROOT / ".local/android/goal-xhs-top20/20260908-223619-e1489c0cbf/source"
        if not base.exists():
            self.skipTest("本机小红书广告与偏移证据缺失")
        first = parse_page((base / "frame-000-before.xml").read_text(encoding="utf-8"), "xiaohongshu", "results", "零跑")
        with self.assertRaises(EvidenceError):
            RankLedger(20).add(dict(page=first))
        page = parse_page((base / "frame-002-before.xml").read_text(encoding="utf-8"), "xiaohongshu", "results", "零跑")
        ad = next(i for i in page["items"] if i["author"] == "卖零跑的小王")
        self.assertTrue(ad["promoted"])
        self.assertTrue(ad["complete"])


class LongImageTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".local/android/goal-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.scene = Image.frombytes("RGB", (320, 1900), random.Random(12).randbytes(320 * 1900 * 3))
        self.addCleanup(self.scene.close)

    def frame(self, shift=0, index=0):
        screen = self.scene.crop((0, shift, 320, shift + 800))
        screen.paste((250, 250, 250), (0, 0, 320, 80))
        path = self.base / f"source-{index}.png"
        screen.save(path)
        xml = self.base / f"source-{index}.xml"
        xml.write_text("<hierarchy/>", encoding="utf-8")
        items = []
        for n in range(9):
            top, bottom = 100 + n * 180 - shift, 260 + n * 180 - shift
            if bottom <= 80 or top >= 800:
                continue
            items.append(dict(identity=f"content-{n}", title=f"标题{n}", author="作者", duration="1:00",
                identity_kind="visible_title_author_duration", promoted=n == 2,
                bounds=[0, max(80, top), 320, min(800, bottom)], complete=top >= 80 and bottom < 800))
        page = dict(items=items, excluded=[], unknown=[], viewport=[0, 80, 320, 800], recognized=True,
                    scope_verified=True, keyword_verified=True, complete=False, end_marker=None)
        return dict(png_path=str(path), xml_path=str(xml), page=page)

    def test_multiframe_exact_pixels_order_and_immutable_source(self):
        ledger = RankLedger(7)
        frames = [self.frame(shift=i * 180, index=i) for i in range(5)]
        hashes = [hashlib.sha256(Path(f["png_path"]).read_bytes()).hexdigest() for f in frames]
        for frame in frames:
            ledger.add(frame)
        self.assertTrue(ledger.target_ready())
        result = render_long(ledger, self.base / "derived.png", 7)
        self.assertEqual(result["height"], 1340)
        self.assertEqual(len(ledger.items), 7)
        self.assertEqual([i["rank"] for i in ledger.items], list(range(1, 8)))
        self.assertTrue(all(i["source_mappings"] for i in ledger.items))
        with Image.open(result["path"]) as image:
            self.assertEqual(image.crop((0, 80, 320, 1340)).tobytes(), self.scene.crop((0, 80, 320, 1340)).tobytes())
        self.assertEqual(hashes, [hashlib.sha256(Path(f["png_path"]).read_bytes()).hexdigest() for f in frames])

    def test_identical_frame_is_not_end_or_progress(self):
        a, b = self.frame(), self.frame(index=1)
        with self.assertRaisesRegex(EvidenceError, "stationary"):
            match_frames(a, b)

    def test_no_common_identity_rejected(self):
        a, b = self.frame(), self.frame(180, 1)
        for i in b["page"]["items"]:
            i["identity"] += "different"
        with self.assertRaisesRegex(EvidenceError, "anchors"):
            match_frames(a, b)

    def test_pixel_content_changed_despite_xml_anchors(self):
        a, b = self.frame(), self.frame(180, 1)
        with Image.open(b["png_path"]) as image:
            image.paste((0, 0, 0), (30, 200, 300, 400))
            image.save(b["png_path"])
        with self.assertRaisesRegex(EvidenceError, "pixel_overlap"):
            match_frames(a, b)

    def test_shift_disagreement_rejected(self):
        a, b = self.frame(), self.frame(180, 1)
        b["page"]["items"][1]["bounds"][1] += 10
        with self.assertRaisesRegex(EvidenceError, "reordered"):
            match_frames(a, b)

    def test_cancel_during_algorithm(self):
        a, b = self.frame(), self.frame(180, 1)
        def cancel():
            raise RuntimeError("cancel_test")
        with self.assertRaisesRegex(RuntimeError, "cancel_test"):
            match_frames(a, b, cancel)

    def test_no_unknown_before_target_boundary(self):
        a = self.frame()
        a["page"]["unknown"] = [dict(bounds=[0, 85, 320, 98], reason="unrecognized")]
        ledger = RankLedger(3)
        ledger.add(a)
        self.assertFalse(ledger.target_ready())
        with self.assertRaises(EvidenceError):
            render_long(ledger, self.base / "should-not-exist.png", 3)

    def test_unrecognized_after_target_does_not_invalidate_top3(self):
        a = self.frame()
        a["page"]["unknown"] = [dict(bounds=[0, 720, 320, 800], reason="unrecognized")]
        ledger = RankLedger(3)
        ledger.add(a)
        self.assertTrue(ledger.target_ready())


if __name__ == "__main__":
    unittest.main()
