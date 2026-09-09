"""本轮新失败定点：只读已保存 XML 与纯函数，不连接设备、不重放采集或历史拼图。"""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from opinion_capture.android_results import (
    END_TEXTS, EvidenceError, bounds, match_frames, page_signature, parse_page,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".local/completion/xiaohongshu/20260908-225756-ad4b0a30d9/source"
FRAMES = (11, 14, 15)


@unittest.skipUnless(all((SOURCE / f"frame-{i:03d}-before.xml").is_file() for i in FRAMES),
                     "本轮新失败 XML 仅本地保留，缺证据时跳过而非伪造实拍")
class SavedNoteTitleTests(unittest.TestCase):
    """复用本轮三份相关 XML；派生变体只在内存改变单一失败条件。"""

    @classmethod
    def setUpClass(cls):
        """一次读取相关帧，并保留原始 XML/PNG 哈希供结束时确认未改写。"""
        cls.xml = {i: (SOURCE / f"frame-{i:03d}-before.xml").read_text(encoding="utf-8") for i in FRAMES}
        cls.pages = {i: parse_page(xml, "xiaohongshu", "results", "零跑") for i, xml in cls.xml.items()}
        paths = [SOURCE / f"frame-{i:03d}{suffix}" for i in FRAMES for suffix in ("-before.xml", ".png")]
        cls.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}

    @classmethod
    def tearDownClass(cls):
        """确认读取的原始证据字节未变化；不保存或重编码图片。"""
        for path, digest in cls.hashes.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise AssertionError(f"原始证据被改写：{path}")

    @staticmethod
    def item(page, author):
        """选取唯一作者对应笔记，失败时保留断言上下文。"""
        items = [i for i in page["items"] if i["author"] == author]
        if len(items) != 1:
            raise AssertionError(f"作者 {author!r} 匹配到 {len(items)} 条笔记")
        return items[0]

    def edited_note(self, frame=14):
        """返回内存 XML、真实正文节点与其容器，避免靠混淆资源名定位。"""
        root = ET.fromstring(self.xml[frame])
        parents = {c: n for n in root.iter() for c in n}
        title = next(n for n in root.iter("node") if n.get("text", "").startswith("零跑新车主看过来啦"))
        return root, title, parents[title]

    @staticmethod
    def parse(root):
        """派生 XML 只交给解析纯函数，保持磁盘证据不变。"""
        return parse_page(ET.tostring(root, encoding="unicode"), "xiaohongshu", "results", "零跑")

    def test_saved_video_body_and_duration_are_distinct(self):
        for frame, author, duration, prefix in (
            (11, "正经撩车", "0:59", "谁都不看好的零跑"),
            (14, "家有一宝", "0:36", "零跑新车主看过来啦"),
        ):
            with self.subTest(frame=frame):
                item = self.item(self.pages[frame], author)
                self.assertTrue(item["title"].startswith(prefix))
                self.assertEqual(item["duration"], duration)
                self.assertEqual(item["identity_kind"], "visible_title_author")
                self.assertTrue(item["complete"])
                self.assertFalse(item["promoted"])
                self.assertFalse(any(i["title"] in ("0:59", "0:36") for i in self.pages[frame]["items"]))

    def test_duration_visibility_and_promotion_do_not_change_identity(self):
        original = self.item(self.pages[14], "家有一宝")
        for keep_duration in (True, False):
            root, _, body = self.edited_note()
            duration = next(n for n in body if n.get("text") == "0:36")
            badge = deepcopy(duration)
            badge.set("text", "推广")
            body.insert(0, badge)
            if keep_duration:
                duration.set("text", "0:37")
            else:
                body.remove(duration)
            page = self.parse(root)
            item = self.item(page, "家有一宝")
            self.assertEqual(item["identity"], original["identity"])
            self.assertEqual(item["title"], original["title"])
            self.assertEqual(item["duration"], "0:37" if keep_duration else "")
            self.assertTrue(item["promoted"])
            self.assertNotEqual(page_signature(page), page_signature(self.pages[14]))
        self.assertEqual(original["identity"], self.item(self.pages[15], "家有一宝")["identity"])

    def test_same_author_and_duration_with_different_body_has_distinct_identity(self):
        root, title, _ = self.edited_note()
        title.set("text", "同一作者的另一条正文，时长仍为0:36")
        item = self.item(self.parse(root), "家有一宝")
        original = self.item(self.pages[14], "家有一宝")
        self.assertEqual((item["author"], item["duration"]), (original["author"], original["duration"]))
        self.assertNotEqual(item["identity"], original["identity"])

    def test_missing_duration_only_or_ambiguous_body_stays_unknown(self):
        box = self.item(self.pages[14], "家有一宝")["bounds"]
        for mode in ("missing", "duration_only", "ambiguous"):
            with self.subTest(mode=mode):
                root, title, body = self.edited_note()
                if mode == "missing":
                    body.remove(title)
                elif mode == "duration_only":
                    title.set("text", "0:36")
                else:
                    extra = deepcopy(title)
                    extra.set("text", "另一候选正文")
                    body.append(extra)
                page = self.parse(root)
                self.assertFalse(any(i["bounds"] == box for i in page["items"]))
                self.assertTrue(any(i["bounds"] == box and i["reason"] == "unidentified_note_card" for i in page["unknown"]))
                self.assertIsNone(page["end_marker"])

    def test_saved_ad_badge_and_body_words_are_not_titles_or_promotion_guesses(self):
        ads = [i for i in self.pages[11]["items"] if i["promoted"]]
        self.assertEqual(len(ads), 1)
        self.assertTrue(ads[0]["title"].startswith("零跑A10|6万+全包带走"))
        self.assertEqual(ads[0]["duration"], "")
        root, title, _ = self.edited_note()
        title.set("text", "分享对广告、推广和品牌合作的看法")
        item = self.item(self.parse(root), "家有一宝")
        self.assertEqual(item["title"], title.get("text"))
        self.assertFalse(item["promoted"])

    def test_note_body_equal_to_end_text_is_content_not_page_end(self):
        for text in sorted(END_TEXTS):
            with self.subTest(text=text):
                root, title, _ = self.edited_note()
                title.set("text", text)
                page = self.parse(root)
                self.assertEqual(self.item(page, "家有一宝")["title"], text)
                self.assertIsNone(page["end_marker"])

    def test_standalone_end_text_without_note_structure_remains_end_evidence(self):
        root = ET.fromstring(self.xml[15])
        container = next(n for n in root.iter("node") if n.get("class", "").endswith("RecyclerView"))
        row = ET.SubElement(container, "node", {"class": "android.widget.TextView", "text": "没有更多了",
                            "package": "com.xingin.xhs", "bounds": "[0,1600][1080,1660]"})
        self.assertEqual(self.parse(root)["end_marker"], dict(text="没有更多了", bounds=bounds(row)))

    def test_saved_shortcut_shrinks_viewport_without_mutating_evidence_bounds(self):
        page = self.pages[14]
        self.assertEqual(page["viewport"], [0, 210, 1080, 1721])
        self.assertEqual(len(page["occlusions"]), 1)
        overlay = page["occlusions"][0]
        self.assertEqual(overlay["reason"], "search_shortcut_prompt")
        self.assertEqual(overlay["bounds"], [26, 1721, 1054, 1833])
        self.assertIn("搜索一键直达", overlay["texts"])
        self.assertIn("去添加", overlay["texts"])
        self.assertTrue(any(t.startswith("把小组件放进你的桌面") for t in overlay["texts"]))
        self.assertFalse(any(i["title"] in overlay["texts"] for i in page["items"]))
        self.assertTrue(all(i["bounds"][3] < 1721 for i in page["anchors"]))
        self.assertTrue(any(i["bounds"] == [8, 1442, 536, 1920] for i in page["unknown"]))
        self.assertTrue(all(i["bounds"][3] < 1721 for i in page["items"] if i["complete"]))
        self.assertEqual(self.pages[15]["viewport"][3], 1920)
        self.assertEqual(self.pages[15]["occlusions"], [])

    def test_prompt_words_in_note_do_not_create_occlusion(self):
        root, title, _ = self.edited_note(frame=15)
        title.set("text", "搜索一键直达 把小组件放进你的桌面 去添加")
        page = self.parse(root)
        self.assertEqual(self.item(page, "家有一宝")["title"], title.get("text"))
        self.assertEqual(page["occlusions"], [])
        self.assertEqual(page["viewport"][3], 1920)

    def test_signature_includes_occlusions_even_with_same_viewport(self):
        page = deepcopy(self.pages[14])
        page["occlusions"] = []
        self.assertNotEqual(page_signature(page), page_signature(self.pages[14]))

    def test_saved_prompt_disappearance_is_no_scroll_not_a_link_or_page_end(self):
        # 不传 png_path：严格零位移在像素拼接前分类，不重算任何历史拼接。
        for frame in (14, 15):
            self.assertIsNone(self.pages[frame]["end_marker"])
        with self.assertRaisesRegex(EvidenceError, "^no_scroll_progress$"):
            match_frames(dict(page=self.pages[14]), dict(page=self.pages[15]))


class NoScrollClassificationTests(unittest.TestCase):
    """仅覆盖新增零位移分支边界，不重跑旧像素测试或成功拼接。"""

    @staticmethod
    def pages(native=False):
        """构造三个相隔足够远的唯一锚点，供静止/歧义条件的纯函数检查。"""
        anchors = [dict(identity=str(i), bounds=[10, 100 + i * 150, 200, 160 + i * 150], complete=True) for i in range(3)]
        page = dict(items=[] if native else anchors, anchors=anchors if native else [], viewport=[0, 0, 600, 1000])
        return page, deepcopy(page)

    def test_all_reliable_anchors_must_be_strictly_stationary(self):
        for native in (False, True):
            a, b = self.pages(native)
            with self.subTest(native=native), self.assertRaisesRegex(EvidenceError, "^no_scroll_progress$"):
                match_frames(dict(page=a), dict(page=b))
        a, b = self.pages()
        a["anchors"] = [dict(identity="extra", bounds=[10, 600, 200, 650])]
        b["anchors"] = [dict(identity="extra", bounds=[10, 595, 200, 645])]
        with self.assertRaisesRegex(EvidenceError, "^stationary_or_reordered_frames$"):
            match_frames(dict(page=a), dict(page=b))

    def test_zero_majority_does_not_hide_a_moving_native_anchor(self):
        a, b = self.pages(native=True)
        a["anchors"].append(dict(identity="outlier", bounds=[10, 650, 200, 690]))
        b["anchors"].append(dict(identity="outlier", bounds=[10, 640, 200, 680]))
        with self.assertRaisesRegex(EvidenceError, "^stationary_or_reordered_frames$"):
            match_frames(dict(page=a), dict(page=b))

    def test_insufficient_native_anchors_are_not_no_scroll(self):
        a, b = self.pages(native=True)
        a["anchors"].pop()
        b["anchors"].pop()
        with self.assertRaisesRegex(EvidenceError, "^ambiguous_native_text_anchors$"):
            match_frames(dict(page=a), dict(page=b))

    def test_mixed_negative_horizontal_and_resized_are_not_no_scroll(self):
        for mode in ("mixed", "negative", "horizontal", "resized"):
            a, b = self.pages()
            for index, item in enumerate(b["items"]):
                box = item["bounds"]
                if mode == "negative" or (mode == "mixed" and index == 0):
                    box[1] += 5
                    box[3] += 5
                elif mode == "horizontal":
                    box[0] += 5
                    box[2] += 5
                elif mode == "resized":
                    box[3] += 5
            with self.subTest(mode=mode), self.assertRaisesRegex(EvidenceError, "^stationary_or_reordered_frames$"):
                match_frames(dict(page=a), dict(page=b))

    def test_positive_shift_still_enters_existing_pixel_validation(self):
        a, b = self.pages()
        for item in b["items"]:
            item["bounds"][1] -= 1
            item["bounds"][3] -= 1
        with patch("opinion_capture.android_results.Image.open", side_effect=RuntimeError("pixel_validation_entered")) as open_image:
            with self.assertRaisesRegex(RuntimeError, "^pixel_validation_entered$"):
                match_frames(dict(page=a, png_path="unused-a"), dict(page=b, png_path="unused-b"))
            open_image.assert_called_once_with("unused-a")


if __name__ == "__main__":
    unittest.main()
