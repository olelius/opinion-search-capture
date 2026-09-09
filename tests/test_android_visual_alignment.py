"""仅覆盖本次视觉位移、计数边界及启动提示修复，不重跑平台采集。"""
from pathlib import Path
import random
import tempfile
import unittest

from PIL import Image

from opinion_capture.android import _is_bilibili_mode_notice
from opinion_capture.android_results import EvidenceError, RankLedger, match_visual_frames


class VisualAlignmentFixTests(unittest.TestCase):
    def test_original_pixel_shift_with_different_viewport_tops(self):
        """不同页头高度和奇数位移仍返回原像素位移；静止帧不能新增内容。"""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            content = Image.frombytes("L", (120, 1000), random.Random(19).randbytes(120000))
            paths = [base / "first.png", base / "second.png"]
            content.crop((0, 0, 120, 600)).save(paths[0])
            content.crop((0, 137, 120, 737)).save(paths[1])
            first = {"png_path": str(paths[0]), "page": {"viewport": [0, 100, 120, 580]}}
            second = {"png_path": str(paths[1]), "page": {"viewport": [0, 64, 120, 580]}}
            link = match_visual_frames(first, second)
            self.assertEqual(link["shift"], 137)
            self.assertEqual(link["mean_error"], 0)
            with self.assertRaisesRegex(EvidenceError, "no_scroll_progress"):
                match_visual_frames(first, first)
            Image.new("L", (120, 600), 255).save(paths[0])
            with self.assertRaisesRegex(EvidenceError, "visual_overlap_ambiguous"):
                match_visual_frames(first, first)

    def test_visual_mode_keeps_unknown_card_boundary(self):
        """视觉对齐不代替独立卡片计数，目标之前的未知卡仍阻止报齐TOP N。"""
        ledger = RankLedger(1, strict=False)
        ledger.frames = [{"page": {"reading_order": "visible_top_y_then_x"}}]
        ledger.items = [{"content_bounds": [0, 200, 120, 400], "source_mappings": []}]
        ledger.unknown = [{"bounds": [0, 100, 120, 180], "offset": 0, "edge": False}]
        self.assertFalse(ledger.target_ready())
        ledger.unknown = []
        self.assertTrue(ledger.target_ready())

    def test_startup_notice_is_not_enabled_mode_or_another_app(self):
        """仅匹配三个明确控件，保护已开启模式及其他页面。"""
        xml = '<hierarchy>' + ''.join(
            f'<node package="tv.danmaku.bili" resource-id="tv.danmaku.bili:id/{rid}" text="{text}" />'
            for rid, text in (("title", "未成年人模式"), ("text2", "进入未成年人模式 &gt;"), ("button", "不再提醒"))
        ) + '</hierarchy>'
        self.assertTrue(_is_bilibili_mode_notice(xml))
        self.assertFalse(_is_bilibili_mode_notice(xml.replace("不再提醒", "输入密码")))
        self.assertFalse(_is_bilibili_mode_notice(xml.replace('package="tv.danmaku.bili"', 'package="another.app"')))


if __name__ == "__main__":
    unittest.main()
