"""XHS 输出闸门定点：仅用临时小帧走实际 _collect，不启动浏览器。

不依赖历史 C11 私有产物。人工三卡六帧复现顶部覆盖缺口与零步长停滞，
只将浏览器等待/截图边界替换为本地数据，保留证据分析、落盘及图像合成。
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from opinion_capture import runner
from opinion_capture import web_xiaohongshu as xhs
from opinion_capture.config import Plan


KEYWORD = "C11-fixture"
STAMP = "2026-09-09T16:20:00+08:00"
SIZE = (64, 80)


def _card(number, y, height, coverage):
    """构造身份、图像和相对覆盖区间均明确的人工卡片。"""
    identity = f"{number:024x}"
    return dict(id=identity, href=f"https://www.xiaohongshu.com/explore/{identity}",
                title=f"人工笔记{number}", text=f"人工笔记{number}",
                rect=[4, y, 48, height], images_loaded=True, cover_present=True,
                coverage=coverage, promoted=False)


def _snapshot(top, items, *, suggestions=False, panel_complete=True):
    """返回与 64×80 原生测试帧匹配的快照；URL 仅作来源校验字符串。"""
    return dict(schema_version=2,
                url=f"https://www.xiaohongshu.com/search_result?keyword={KEYWORD}",
                keyword=KEYWORD, input_visible=True, input_id="search-input",
                results=items, feed_count=1, search_tab="综合",
                viewport=dict(width=SIZE[0], height=SIZE[1]),
                scroll=dict(top=top, height=240, viewport_height=80,
                            at_top=top == 0, at_bottom=False, rect=[0, 0, 64, 80]),
                loading=False, pending_images=0, explicit_end=False,
                suggestions=[dict(text="人工下拉词", rect=[4, 20, 48, 20])] if suggestions else [],
                suggestion_panel_complete=panel_complete,
                suggestion_panel_associated=True, suggestion_pending_images=0)


def _diagnostic_states():
    """第二卡首段始终缺失：64px 正常滚动、40px 末段滚动后停留拍摄。"""
    states = []
    for top in (0, 64, 104, 104, 104, 104):
        items = [_card(1, 16-top, 32, [0, 32] if top == 0 else None),
                 _card(2, 72-top, 40, [8, 40] if top == 64 else None),
                 _card(3, 140-top, 36, None if top == 0 else [0, 4] if top == 64 else [0, 36])]
        states.append(_snapshot(top, items))
    return states


class XhsDiagnosticOutputTests(unittest.TestCase):
    """隔离诊断图、验证成功长图，并保护独立的下拉词合同。"""

    def _collect_fixture(self, run, states, *, kind="results", top_n=3, override=None):
        """使用真实 _collect/_evaluate/_checkpoint；override 仅构造矛盾状态的闸门输入。"""
        run.mkdir()
        request = xhs.validate_request(dict(keyword=KEYWORD, capture_kind=kind,
                                             top_n=top_n, output_dir=str(run)))
        result = xhs._base_result()
        result.update(keyword=KEYWORD, capture_kind=kind, requested_top_n=top_n)
        evidence = dict(frames=[], evidence_source="offline_small_frame_fixture")
        page = Mock(spec_set=["evaluate"])
        evaluate = xhs._evaluate

        def save_frame(_page, directory, frames, _request, _budget, _http_status):
            """在临时目录保存小 PNG/HTML，并按真实保存边界登记不可变源帧。"""
            index = len(frames)
            state = deepcopy(states[index])
            path = directory / f"frame-{index:03d}.png"
            with Image.new("RGB", SIZE) as image:
                image.putdata([(x, y*3, index*30) for y in range(SIZE[1]) for x in range(SIZE[0])])
                image.save(path)
            html = path.with_suffix(".html")
            html.write_text("<!doctype html><title>offline fixture</title>", encoding="utf-8")
            frame = dict(path=str(path), html_path=str(html),
                         sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         html_sha256=hashlib.sha256(html.read_bytes()).hexdigest(),
                         dimensions=list(SIZE), captured_at_range=[STAMP, STAMP],
                         before=deepcopy(state), after=state, stable=True)
            frames.append(frame)
            return frame

        def assess(frames, req, stop_reason=""):
            """先执行真实证据分析，再按需构造 status/rank 不一致的防御性用例。"""
            analysis = evaluate(frames, req, stop_reason)
            if override is not None:
                analysis.update(status=override[0], rank_verified=override[1])
            return analysis

        with patch.object(xhs, "_settled", side_effect=lambda *args: deepcopy(states[len(evidence["frames"])])), \
                patch.object(xhs, "_save_frame", side_effect=save_frame), \
                patch.object(xhs, "_evaluate", side_effect=assess), \
                patch.object(xhs, "_compose_primary", wraps=xhs._compose_primary) as compose:
            xhs._collect(page, request, run, result, evidence, xhs.Budget(10))
        return result, evidence, compose

    def _assert_source_frames_intact(self, run, result, evidence, expected_count):
        """核对原帧数量、实际文件哈希及最终 evidence 的持久化引用。"""
        self.assertEqual(len(evidence["frames"]), expected_count)
        self.assertEqual(result["source_artifact_paths"], [f["path"] for f in evidence["frames"]])
        for frame in evidence["frames"]:
            for path_key, hash_key in (("path", "sha256"), ("html_path", "html_sha256")):
                self.assertIn(frame[path_key], result["artifact_paths"])
                self.assertEqual(hashlib.sha256(Path(frame[path_key]).read_bytes()).hexdigest(), frame[hash_key])
            with Image.open(frame["path"]) as source:
                self.assertEqual(source.size, SIZE)
                source.verify()
        saved = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["result"]["source_artifact_paths"], result["source_artifact_paths"])
        self.assertEqual(saved["result"]["primary_artifact_path"], result["primary_artifact_path"])
        self.assertEqual(saved["evidence"]["artifact_role"], evidence["artifact_role"])

    def test_results_diagnostic_frames_skip_primary_and_composite(self):
        """partial 或排名未证实时只留原帧；两字段必须同时满足，才开放主图出口。"""
        for override in (None, ("success", False), ("partial", True)):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp) / "capture"
                result, evidence, compose = self._collect_fixture(run, _diagnostic_states(), override=override)
                expected_count = 1 if override == ("success", False) else 6
                self.assertIsNone(result["primary_artifact_path"])
                compose.assert_not_called()
                self.assertFalse((run / "primary.png").exists())
                self.assertEqual(evidence["artifact_role"], "diagnostic")
                self.assertNotIn("primary_layout", evidence)
                self._assert_source_frames_intact(run, result, evidence, expected_count)
                if override is None:
                    self.assertEqual((result["status"], result["rank_verified"], result["captured_count"]),
                                     ("partial", False, 2))
                    self.assertIn("target_images_or_screenshot_coverage_incomplete", result["reason"])
                    self.assertEqual(result["stop_reason"], "scroll_stalled_without_complete_evidence")
                    self.assertEqual([f.get("next_scroll_step") for f in evidence["frames"]],
                                     [64, 40, 0, 0, 0, None])
                plan = Plan(output_root=str(Path(tmp) / "exports"), keywords=[KEYWORD],
                            platforms=["xiaohongshu"], clients=["WEB"], capture_kinds=["results"],
                            top_n=3, max_retries=0)
                store = Mock()
                task = dict(id="fixture-task", platform="xiaohongshu", client="WEB",
                            capture_kind="results", keyword=KEYWORD)
                batch = dict(id="fixture-batch", sequence=1)
                with patch.object(runner, "copy_immutable") as export, \
                        patch("opinion_capture.imaging.compose_pages") as summary:
                    runner._export_primary(plan, store, batch, task, dict(id="fixture-attempt"),
                                           run, result, datetime.fromisoformat(result["captured_at"]))
                    composites = runner._make_composites(plan, store, batch, [result], "fixture-run", None)
                self.assertEqual(result["exports"], [])
                self.assertEqual(composites, [])
                export.assert_not_called()
                summary.assert_not_called()
                store.add_artifact.assert_not_called()
                store.add_composite.assert_not_called()

    def test_verified_results_keep_continuous_primary(self):
        """成功排名仍生成原像素连续长图，而不是把两张整屏堆叠。"""
        states = [_snapshot(0, [_card(1, 16, 112, [0, 64])]),
                  _snapshot(56, [_card(1, -40, 112, [40, 112])])]
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "capture"
            result, evidence, compose = self._collect_fixture(run, states, top_n=1)
            self.assertEqual((result["status"], result["rank_verified"]), ("success", True))
            compose.assert_called_once()
            self.assertIs(compose.call_args.args[3], result)
            self.assertEqual(evidence["artifact_role"], "target_observation")
            self.assertEqual(evidence["composition"], "continuous_native_scroll_column")
            self._assert_source_frames_intact(run, result, evidence, 2)
            primary = Path(result["primary_artifact_path"])
            self.assertNotIn(str(primary), result["source_artifact_paths"])
            self.assertEqual(result["artifact_paths"].count(str(primary)), 1)
            self.assertEqual(hashlib.sha256(primary.read_bytes()).hexdigest(), evidence["primary_sha256"])
            with Image.open(primary) as image:
                self.assertEqual(image.size, (64, 128))
                self.assertEqual(len(evidence["primary_layout"]), 2)
                for placement in evidence["primary_layout"]:
                    with Image.open(placement["source_path"]) as source:
                        self.assertEqual(image.crop(placement["output_box"]).tobytes(),
                                         source.crop(placement["source_box"]).tobytes())

    def test_suggestions_keep_native_primary_without_rank_proof(self):
        """下拉词不要求 rank_verified；保留成功与已绑定 partial 的单屏输出合同。"""
        for complete in (True, False):
            with self.subTest(panel_complete=complete), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp) / "capture"
                states = [_snapshot(0, [], suggestions=True, panel_complete=complete)]
                result, evidence, compose = self._collect_fixture(run, states, kind="suggestions")
                self.assertEqual(result["status"], "success" if complete else "partial")
                self.assertFalse(result["rank_verified"])
                compose.assert_called_once()
                self.assertIsNone(compose.call_args.args[3])
                self.assertEqual(evidence["composition"], "native_viewport_frames")
                self.assertEqual(evidence["artifact_role"], "target_observation")
                self._assert_source_frames_intact(run, result, evidence, 1)
                with Image.open(result["primary_artifact_path"]) as primary, \
                        Image.open(result["source_artifact_paths"][0]) as source:
                    self.assertEqual(primary.size, SIZE)
                    self.assertEqual(primary.tobytes(), source.tobytes())


if __name__ == "__main__":
    unittest.main()
