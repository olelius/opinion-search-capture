"""80%滚动修复的定点检查；合成像素不是平台实拍，测试不连接设备。"""
from copy import deepcopy
import json
from pathlib import Path
import random
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from PIL import Image

from opinion_capture.android import AndroidBlocked, _swipe_forward
from opinion_capture.android_results import (
    EvidenceError, RankLedger, bounds, match_visual_frames, render_long, xhs_query_nodes,
)


class RecordingAdb:
    """只记录手势命令，不调用ADB。"""
    def __init__(self):
        self.deadline = time.monotonic() + 10
        self.cancel_file = None
        self.serial = "fixture"
        self.commands = []

    def run(self, *command):
        self.commands.append(command)


class Scroll80SliceTests(unittest.TestCase):
    def test_saved_high_resolution_smooth_photo_and_white_placeholder_are_distinct(self):
        from opinion_capture.android_image_evidence import assess_cover
        root = Path(__file__).resolve().parents[1] / ".local/android/scroll80-slice-20260909-124300/xiaohongshu"
        photo = root / "20260909-125730-0d08471193/source/frame-002.png"
        blank = root / "20260909-124546-47c598d387/source/frame-000.png"
        if not photo.exists() or not blank.exists():
            self.skipTest("本机问题源图不随仓库分发")
        good = assess_cover(photo, [544, 444, 1072, 840])
        self.assertLess(good["edge_energy"], good["thresholds"]["edge_energy"])
        self.assertGreater(good["normalized_edge_energy"], good["thresholds"]["edge_energy"])
        self.assertEqual(good["status"], "content_visible")
        self.assertEqual(assess_cover(blank, [8, 1026, 536, 1423])["status"], "blank_or_unverified")

    def test_foreground_search_entry_never_relaunches_or_clicks_submit_as_fallback(self):
        from unittest.mock import MagicMock
        from opinion_capture.android import _navigate, PACKAGES
        root = Path(__file__).resolve().parents[1]
        path = root / ".local/android/scroll80-slice-20260909-124300/xiaohongshu/20260909-125532-6beffa31d0/hierarchy.xml"
        if not path.exists():
            self.skipTest("本机失败XML不随仓库分发")
        device, edit, query, submit = (MagicMock() for _ in range(4))
        device.app_current.return_value = {"package": PACKAGES["xiaohongshu"]}
        device.dump_hierarchy.return_value = path.read_text(encoding="utf-8")
        edit.exists = False
        edit.wait.return_value = False
        query.count = 1
        def select(**kwargs):
            if kwargs.get("className") == "android.widget.EditText": return edit
            return query if kwargs.get("text") == "零跑" else submit
        device.side_effect = select
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(AndroidBlocked, "search_entry_not_identified"):
            _navigate(device, PACKAGES["xiaohongshu"], "零跑", "results", time.monotonic() + 10, navigation_dir=directory)
        device.app_start.assert_not_called()
        query.click.assert_called_once()
        submit.click.assert_not_called()

    def test_full_distance_for_both_apps_overlay_and_cautious(self):
        for platform in ("xiaohongshu", "bilibili"):
            for cautious in (False, True):
                for viewport in ([0, 210, 1080, 1721], [40, 400, 2200, 3700], [0, 0, 200, 200]):
                    with self.subTest(platform=platform, cautious=cautious, viewport=viewport):
                        adb = RecordingAdb()
                        action = _swipe_forward(adb, dict(platform=platform, viewport=viewport), cautious=cautious)
                        height = viewport[3] - viewport[1]
                        self.assertEqual(action["start"][1] - action["end"][1], round(height * .8))
                        self.assertTrue(viewport[1] < action["end"][1] < action["start"][1] < viewport[3])
                        self.assertAlmostEqual(action["expected_overlap_ratio"], .2, delta=1 / height)
                        command = adb.commands[0]
                        self.assertEqual(int(command[4]) - int(command[6]), round(height * .8))
                        self.assertLessEqual(action["nominal_speed_px_s"], 450)
                        self.assertEqual(int(command[-1]), action["duration_ms"])

    def test_boundary_shortens_but_normal_distance_has_no_pixel_cap(self):
        adb = RecordingAdb()
        page = dict(platform="bilibili", viewport=[0, 200, 1080, 1900], items=[dict(complete=True)] * 5)
        normal = _swipe_forward(adb, page, remaining=15)
        tail = _swipe_forward(adb, page, remaining=1)
        self.assertEqual(normal["target_px"], 1360)
        self.assertEqual(tail["target_px"], 272)
        self.assertTrue(tail["boundary_limited"])
        self.assertFalse(normal["boundary_limited"])

    def test_invalid_viewport_never_dispatches(self):
        adb = RecordingAdb()
        with self.assertRaises(AndroidBlocked):
            _swipe_forward(adb, dict(viewport=[0, 200, 1080, 250]))
        self.assertEqual(adb.commands, [])

    def test_interrupted_gesture_keeps_target_but_never_claims_content_displacement(self):
        adb, record = RecordingAdb(), {}
        with patch.object(adb, "run", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
            _swipe_forward(adb, dict(viewport=[0, 201, 1080, 1920]), record=record)
        self.assertEqual(record["target_px"], 1375)
        self.assertEqual(record["dispatch_status"], "interrupted_or_unknown")
        self.assertNotIn("verified_content_shift_px", record)

    def test_parent_capture_reports_four_phases_without_extra_device_requests(self):
        from opinion_capture.android import capture
        from opinion_capture.progress import phase_scope
        from tests.test_android_capture import document

        class FakeAdb:
            """单帧成功的设备替身，仅允许原路径中的发现与截图。"""
            def __init__(self, deadline, cancel_file=None):
                self.deadline, self.cancel_file, self.serial = deadline, cancel_file, "fixture"

            def discover(self, serial):
                return {"serial": self.serial}

            def installed(self, package):
                return True

            def screenshot(self, path):
                Image.new("RGB", (320, 800), (20, 50, 70)).save(path)

        with tempfile.TemporaryDirectory() as directory:
            request = dict(platform="bilibili", capture_kind="results", keyword="测试", top_n=3, output_dir=directory)
            with phase_scope(request) as recorder, patch("opinion_capture.android.AdbClient", FakeAdb), \
                    patch("opinion_capture.android._ui_call", return_value=document()) as ui:
                result = capture(request)
            self.assertEqual(result["status"], "success", result["reason"])
            self.assertEqual([r["phase_code"] for r in recorder.timings],
                ["device_ready", "prepare_search", "screenshot", "validate_capture"])
            self.assertEqual([call.args[2] for call in ui.call_args_list], ["navigate", "dump", "dump"])

    def test_saved_search_row_scales_and_moves_without_pixel_gate(self):
        root = Path(__file__).resolve().parents[1]
        summary = root / ".local/completion/app-scrollshot-delivery-20260909-011508/summary.json"
        if not summary.exists():
            self.skipTest("本机原始XML不随仓库分发")
        report = json.loads(Path(json.loads(summary.read_text(encoding="utf-8"))[0]["result_path"]).read_text(encoding="utf-8"))
        tree = ET.parse(report["frames"][0]["xml_path"]).getroot()
        self.assertEqual([n.get("text") for n in xhs_query_nodes(tree)], ["零跑"])
        for node in tree.iter("node"):
            x1, y1, x2, y2 = bounds(node)
            node.set("bounds", f"[{x1*2},{y1*2+350}][{x2*2},{y2*2+350}]")
        selected = xhs_query_nodes(tree)
        self.assertEqual([n.get("text") for n in selected], ["零跑"])
        self.assertGreater(bounds(selected[0])[1], 400)
        for node in tree.iter("node"):
            if node.get("content-desc") == "返回":
                node.set("content-desc", "")
        self.assertEqual(xhs_query_nodes(tree), [])

    def _linked_fixture(self, directory):
        """同一合成画布相隔800px；卡片仅分两帧完整覆盖，身份只在尾帧出现。"""
        base = Path(directory)
        pixels = Image.frombytes("RGB", (240, 2100), random.Random(83).randbytes(240 * 2100 * 3))
        frames = []
        for index, offset in enumerate((0, 800)):
            path = base / f"source-{index}.png"
            pixels.crop((0, offset, 240, offset + 1200)).save(path)
            frames.append(dict(png_path=str(path), xml_path=str(base / f"source-{index}.xml"), source_index=index,
                accepted=False, page=dict(platform="xiaohongshu", viewport=[0, 100, 240, 1100],
                keyword_verified=True, scope_verified=True, recognized=True, start_verified=True,
                reading_order="visible_top_y_then_x", items=[], excluded=[], unknown=[], end_marker=None)))
        frames[0]["page"]["unknown"] = [dict(reason="unidentified_note_card", bounds=[8, 650, 110, 1100],
            cover_bounds=[8, 650, 110, 1100], edge=True)]
        frames[1]["page"]["items"] = [dict(identity="note-a", title="合成标题", author="合成作者", complete=False,
            cover_bounds=[8, 42, 110, 480], bounds=[8, 42, 110, 620])]
        frames[1]["page"]["unknown"] = [dict(reason="incomplete_content_card", identity="note-a",
            bounds=[8, 42, 110, 620], edge=True)]
        return pixels, frames

    def test_80_percent_alignment_and_card_coverage_preserve_source(self):
        with tempfile.TemporaryDirectory() as directory:
            pixels, frames = self._linked_fixture(directory)
            original_pages = deepcopy([f["page"] for f in frames])
            link = match_visual_frames(*frames)
            self.assertEqual(link["shift"], 800)
            self.assertEqual(link["overlap"], 200)
            ledger = RankLedger(1, strict=False)
            for frame in frames:
                ledger.add(frame)
            self.assertTrue(ledger.target_ready())
            self.assertEqual(ledger.items[0]["content_bounds"], [8, 650, 110, 1420])
            self.assertEqual(len(ledger.items[0]["source_mappings"]), 2)
            self.assertEqual([f["page"] for f in frames], original_pages)
            rendered = render_long(ledger, Path(directory) / "long.png", 1)
            with Image.open(rendered["path"]) as actual:
                self.assertEqual(actual.tobytes(), pixels.crop((0, 0, 240, 1420)).tobytes())

    def test_collection_records_command_target_separately_from_measured_content_shift(self):
        from opinion_capture.android import _collect
        with tempfile.TemporaryDirectory() as directory:
            _, frames = self._linked_fixture(directory)
            result = dict(requested_top_n=1, capture_kind="results", frames=[], artifact_paths=[])
            def snapshot(adb, output, result, index):
                frame = frames[index]
                frame["stable"] = True
                result["frames"].append(frame)
                return frame
            with patch("opinion_capture.android._snapshot", side_effect=snapshot), patch("opinion_capture.android.time.sleep"):
                _collect(RecordingAdb(), Path(directory), result, {"max_frames": 2})
            self.assertEqual(result["status"], "success")
            measurement = frames[1]["scroll_measurement"]
            self.assertEqual(measurement["gesture_target_px"], 400)
            self.assertEqual(measurement["verified_content_shift_px"], 800)
            self.assertEqual(measurement["verified_overlap_px"], 200)
            self.assertEqual(measurement["gesture_dispatch_status"], "command_completed")

    def test_missing_fragment_structure_or_identity_never_counts(self):
        for missing in ("cover_bounds", "title", "author"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                _, frames = self._linked_fixture(directory)
                if missing == "cover_bounds":
                    frames[0]["page"]["unknown"][0].pop(missing)
                else:
                    frames[1]["page"]["items"][0].pop(missing)
                ledger = RankLedger(1, strict=False)
                for frame in frames:
                    ledger.add(frame)
                self.assertFalse(ledger.target_ready())
                self.assertEqual(ledger.items, [])

    def test_known_partial_identity_links_only_to_same_identity(self):
        for same_identity in (True, False):
            with self.subTest(same_identity=same_identity), tempfile.TemporaryDirectory() as directory:
                _, frames = self._linked_fixture(directory)
                identity = "note-a" if same_identity else "different-note"
                head = dict(identity=identity, title="合成标题", author="合成作者", complete=False,
                            bounds=[8, 650, 110, 1100], cover_bounds=[8, 650, 110, 1100])
                frames[0]["page"]["items"] = [head]
                frames[0]["page"]["unknown"] = [dict(reason="incomplete_content_card", identity=identity,
                    bounds=head["bounds"], edge=True)]
                ledger = RankLedger(1, strict=False)
                for frame in frames:
                    ledger.add(frame)
                self.assertEqual(ledger.target_ready(), same_identity)
                self.assertEqual(len(ledger.items), int(same_identity))

    def test_unresolved_past_fragment_stops_forward_collection(self):
        from opinion_capture.android import _collect
        with tempfile.TemporaryDirectory() as directory:
            _, frames = self._linked_fixture(directory)
            frames[0]["page"]["unknown"][0].pop("cover_bounds")
            # 第三帧仍识别不出旧片段；用既有第二帧的已知几何下界证明它已完全滚过。
            frames[1]["page"]["unknown"] = []
            result = dict(requested_top_n=20, capture_kind="results", frames=[], artifact_paths=[])
            def snapshot(adb, output, result, index):
                frame = frames[index]
                frame["stable"] = True
                result["frames"].append(frame)
                return frame
            # 人工设置尚未分类片段早于真实内容窗；仍需真实像素位移来确定越过。
            frames[0]["page"]["unknown"][0]["bounds"] = [8, 500, 110, 700]
            adb = RecordingAdb()
            with patch("opinion_capture.android._snapshot", side_effect=snapshot), patch("opinion_capture.android.time.sleep"), \
                    self.assertRaisesRegex(EvidenceError, "unresolved_content_gap_before_viewport"):
                _collect(adb, Path(directory), result, {"max_frames": 3})
            self.assertEqual(len(adb.commands), 1)

    def test_wrong_fragment_pixels_rejected_atomically_even_with_valid_global_shift(self):
        with tempfile.TemporaryDirectory() as directory:
            _, frames = self._linked_fixture(directory)
            with Image.open(frames[1]["png_path"]) as source:
                image = source.copy()
            image.paste((255, 255, 255), (8, 100, 110, 300))
            image.save(frames[1]["png_path"])
            ledger = RankLedger(1, strict=False)
            ledger.add(frames[0])
            with patch("opinion_capture.android_results.match_visual_frames", return_value={"shift": 800}), \
                    self.assertRaisesRegex(EvidenceError, "partial_note_overlap_unverified"):
                ledger.add(frames[1])
            self.assertEqual(ledger.items, [])
            self.assertEqual(ledger.links, [])
            self.assertFalse(frames[1]["accepted"])

    def test_visual_match_rejects_blank_and_insufficient_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            _, frames = self._linked_fixture(directory)
            Image.new("RGB", (240, 1200), "white").save(frames[0]["png_path"])
            Image.new("RGB", (240, 1200), "white").save(frames[1]["png_path"])
            with self.assertRaisesRegex(EvidenceError, "visual_overlap_ambiguous"):
                match_visual_frames(*frames)
            frames[1]["page"]["viewport"] = [0, 1040, 240, 1100]
            with self.assertRaisesRegex(EvidenceError, "visual_overlap_too_small"):
                match_visual_frames(*frames)


if __name__ == "__main__":
    unittest.main()
