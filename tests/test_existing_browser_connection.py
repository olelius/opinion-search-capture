"""现有Chrome连接的新路径定点验证：不启动Chrome、不访问平台或用户资料。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from opinion_capture.existing_browser import _Controller, _Session, worker_main


class ExistingBrowserConnectionTests(unittest.TestCase):
    def test_utf8_pipe_ignores_windows_stdin_codepage(self):
        """重现客户中文目录触发的非法转义；只验证JSON管道，不打开浏览器。"""
        message = {"id":"encoding-regression", "request":{
            "platform":"xiaohongshu", "client":"WEB", "keyword":"三角洲",
            "output_dir":r"D:\实用的板凳\OpinionCapture-Portable\.local\exports"}}
        wire = (json.dumps(message, ensure_ascii=False)+"\n").encode("utf-8")
        with io.TextIOWrapper(io.BytesIO(wire), encoding="cp936", errors="replace") as legacy:
            with self.assertRaises(json.JSONDecodeError):
                json.loads(legacy.readline())
        with io.TextIOWrapper(io.BytesIO(wire), encoding="cp936", errors="replace") as incoming:
            output = io.StringIO()
            with patch("opinion_capture.existing_browser.sys.stdin", incoming), \
                 patch("opinion_capture.existing_browser.sys.stdout", output), \
                 patch("opinion_capture.existing_browser._Session") as factory:
                factory.return_value.capture.return_value = {"status":"success", "keyword":"三角洲"}
                self.assertEqual(worker_main(), 0)
                factory.return_value.capture.assert_called_once_with(message["request"])
                factory.return_value.disconnect.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())["result"]["keyword"], "三角洲")

    def test_reuse_one_connection_and_never_close_user_browser(self):
        session = _Session()
        runtime = MagicMock()
        browser = runtime.chromium.connect_over_cdp.return_value
        browser.is_connected.return_value = True
        context, page = MagicMock(), MagicMock()
        page.url = "https://www.xiaohongshu.com/search_result?keyword=A1"
        page.is_closed.return_value = False
        context.pages, browser.contexts = [page], [context]
        with patch("opinion_capture.existing_browser.resolve_browser_endpoint", return_value="http://127.0.0.1:9222"), \
             patch("playwright.sync_api.sync_playwright") as factory:
            factory.return_value.start.return_value = runtime
            first = session.page({"platform": "xiaohongshu", "keyword": "A1", "timeout_seconds": 5})
            second = session.page({"platform": "xiaohongshu", "keyword": "A10", "timeout_seconds": 5})
            self.assertIs(first, second)
            runtime.chromium.connect_over_cdp.assert_called_once()
            context.new_page.assert_not_called()
            session.disconnect()
        browser.close.assert_not_called()
        context.close.assert_not_called()
        page.close.assert_not_called()
        runtime.stop.assert_called_once()

    def test_real_worker_transport_reuses_process_without_browser_requests(self):
        """固定非法平台入口在连接前返回，检验两轮IPC和回收，不触及真实Chrome。"""
        with tempfile.TemporaryDirectory() as directory:
            controller = _Controller()
            request = {"client": "APP", "platform": "bilibili", "keyword": "A1",
                       "output_dir": str(Path(directory).resolve()), "timeout_seconds": 5}
            try:
                first = controller.capture(request)
                process = controller.process
                second = controller.capture(request)
                self.assertEqual(first["reason"], "existing_browser_request_invalid")
                self.assertEqual(second["reason"], "existing_browser_request_invalid")
                self.assertIs(controller.process, process)
                self.assertIsNone(process.poll())
            finally:
                controller.close()
            self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
