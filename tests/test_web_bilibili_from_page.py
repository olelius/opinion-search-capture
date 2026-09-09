"""借入page入口的定点mock检查；不运行真实浏览器或既有截图算法。"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture import web_bilibili as web


class BorrowedPageTest(unittest.TestCase):
    def test_same_keyword_later_page_navigates_once_before_original_collector(self):
        """同词第2页仅回首页一次；到达原采集器即用mock停止，不运行截图算法。"""
        keyword = "比亚迪"
        first_url = web.build_source_url(keyword, "results")
        later_url = first_url + "&page=2"
        page = Mock(spec_set=["url", "on", "remove_listener", "goto", "locator"])
        page.url = later_url
        page.locator.return_value.first = Mock(spec_set=["wait_for"])
        scene = {"url": later_url, "input_value": keyword, "items": [{"id": "mock-id"}],
                 "page_number": 2}

        def goto(url, **kwargs):
            self.assertEqual(url, first_url)
            self.assertGreater(kwargs["timeout"], 0)
            page.url = url
            scene.update(url=url, page_number=1)
            return Mock(status=200)

        def collect(given_page, request, run, budget, snapshot, *args):
            self.assertIs(given_page, page)
            self.assertEqual(snapshot()["page_number"], 1)
            raise RuntimeError("mock_stop_before_existing_capture")

        page.goto.side_effect = goto
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(web, "_snapshot", side_effect=lambda *args: copy.deepcopy(scene)), \
                patch.object(web, "_dismiss_ordinary_tip_once"), \
                patch.object(web._Budget, "pause", lambda budget, *args: budget()), \
                patch.object(web, "collect_results", side_effect=collect) as collector:
            result = web.capture_from_page(page, {"keyword": keyword, "capture_kind": "results",
                                                 "output_dir": directory, "top_n": 3})
            page.goto.assert_called_once()
            self.assertEqual(page.goto.call_args.args, (first_url,))
            self.assertEqual(page.goto.call_args.kwargs["wait_until"], "domcontentloaded")
            collector.assert_called_once()
            self.assertEqual(result["reason"], "RuntimeError: mock_stop_before_existing_capture")
            self.assertEqual(result["source_url"], first_url)
            evidence_path = next(Path(directory).glob("*/worker-evidence.json"))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["navigation"], {"from_url": later_url, "target_url": first_url,
                                                     "action": "goto", "reason": "return_to_first_page"})
            self.assertEqual(page.on.call_args_list, page.remove_listener.call_args_list)

    def test_same_search_reuses_page_and_keeps_new_evidence_separate(self):
        """同一page两次请求无导航/输入/资源操作，分别返回本次原图及最终图。"""
        keyword = "比亚迪"
        # 严格接口mock不暴露context/cookies/close/default_timeout等生命周期方法。
        page = Mock(spec_set=["url", "on", "remove_listener", "goto", "locator"])
        page.url = web.build_source_url(keyword, "results")
        page.goto.side_effect = AssertionError("同关键词不应再次导航")
        box = Mock(spec_set=["wait_for"])
        page.locator.return_value.first = box
        scene = {"url": page.url, "input_value": keyword, "items": [{"id": "mock-id"}],
                 "page_number": 1}
        forbidden = AssertionError("借用page入口不应启动浏览器、准备profile或创建子进程")

        def collect(given_page, request, run, budget, snapshot, classify, checkpoint, settle_ui):
            """只模拟旧采集器返回合同；占位字节不是平台截图或实拍通过证据。"""
            self.assertIs(given_page, page)
            self.assertGreater(budget(), 0)
            source = run / "page-001-original-v1.png"
            primary = run / "primary-results-v1.png"
            html = run / "page-001-dom-v1.html"
            source.write_bytes(b"mock-only source bytes")
            primary.write_bytes(b"mock-only primary bytes")
            html.write_text("<!-- mock-only DOM -->", encoding="utf-8")
            item = {"id": "mock-id", "rank": 1, "source_artifact_path": str(source)}
            record = {"page_index": 1, "source": {"path": str(source)}, "html_path": str(html),
                      "crop": {"box": [0, 0, 1, 1]}, "occurrences": [item], "dom_stable": True,
                      "capture_started_at": budget.timestamp(), "capture_finished_at": budget.timestamp()}
            checkpoint(record)
            return {"items": [item], "coverage": {}, "pages": [record],
                    "primary": {"path": str(primary)}, "scene": snapshot(), "stopped_reason": ""}

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(web, "_launch", side_effect=forbidden) as launch, \
                patch.object(web, "_prepare_profile", side_effect=forbidden) as profile, \
                patch.object(web.subprocess, "Popen", side_effect=forbidden) as process, \
                patch.object(web, "_snapshot", side_effect=lambda *args: copy.deepcopy(scene)), \
                patch.object(web, "_dismiss_ordinary_tip_once"), \
                patch.object(web._Budget, "pause", lambda budget, *args: budget()), \
                patch.object(web, "collect_results", side_effect=collect) as collector, \
                patch.object(web, "assess_capture", return_value={"status": "success", "reason": "mock-only",
                             "outcome": "complete", "observed_count": 1, "rank_verified": True}):
            request = {"keyword": keyword, "capture_kind": "results", "top_n": 1,
                       "output_dir": directory, "timeout_seconds": 30,
                       "deadline": time.monotonic() + 10,
                       "cancel_file": str(Path(directory) / "cancel"),
                       "user_data_dir": str(Path(directory) / "must-not-be-read")}
            original_request = dict(request)
            first = web.capture_from_page(page, request)
            source = Path(first["source_artifact_paths"][0])
            source_bytes = source.read_bytes()
            second = web.capture_from_page(page, request)
            self.assertEqual(request, original_request)
            self.assertNotEqual(first["primary_artifact_path"], second["primary_artifact_path"])
            self.assertEqual(source.read_bytes(), source_bytes)
            for result in (first, second):
                self.assertEqual(result["status"], "success")
                self.assertTrue(set(web._base_result(request)) <= result.keys())
                primary = Path(result["primary_artifact_path"])
                self.assertEqual(primary.name, "primary-results-v1.png")
                self.assertEqual(result["artifact_paths"][0], str(primary))
                self.assertEqual(primary.parent.parent, Path(directory))
                self.assertEqual(result["source_artifact_paths"],
                                 [str(primary.parent / "page-001-original-v1.png")])
                for path in result["artifact_paths"] + result["source_artifact_paths"]:
                    self.assertTrue(Path(path).is_file())
                evidence = json.loads((primary.parent / "worker-evidence.json").read_text(encoding="utf-8"))
                self.assertEqual(evidence["navigation"]["action"], "reuse")
                self.assertEqual(evidence["page_ownership"], "caller")
                payload = json.loads((primary.parent / "worker-request.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["deadline"], request["deadline"])
                self.assertEqual(payload["request"]["cancel_file"], request["cancel_file"])
                self.assertIsNone(payload["request"]["user_data_dir"])
                self.assertFalse((primary.parent / "profile").exists())
                self.assertFalse((primary.parent / "temp").exists())
            self.assertEqual(collector.call_count, 2)
            page.goto.assert_not_called()
            self.assertEqual(page.on.call_count, 2)
            self.assertEqual(page.on.call_args_list, page.remove_listener.call_args_list)
            launch.assert_not_called()
            profile.assert_not_called()
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
