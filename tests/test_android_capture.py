"""采集器离线流程测试；所有设备动作均为替身，源像素为明确合成测试数据。"""
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PIL import Image

from opinion_capture.android import capture
from opinion_capture.android_results import parse_page


ROOT = Path(__file__).resolve().parents[1]
PKG = "tv.danmaku.bili"


def document(count=3, ended=False):
    """最小语义树，不伪装真实平台实拍。"""
    def node(parent, rid="", text="", box="[0,0][320,800]", cls="android.view.ViewGroup", **attrs):
        return ET.SubElement(parent, "node", {"package": PKG, "class": cls, "resource-id": PKG + ":id/" + rid,
                      "text": text, "bounds": box, **attrs})
    root = ET.Element("hierarchy")
    node(root, "search_fake_text", "测试", "[20,0][300,40]", "android.widget.TextView")
    node(root, "tab_title", "综合", "[0,40][80,80]", "android.widget.TextView", selected="true")
    holder = node(root, "result_container", box="[0,80][320,800]")
    grid = node(holder, "recycler_view", box="[0,80][320,800]", scrollable="true")
    for i in range(count):
        top = 90 + i * 170
        row = node(grid, box=f"[0,{top}][320,{top + 160}]")
        node(row, "cover", box=f"[10,{top+10}][100,{top+140}]", cls="android.widget.ImageView")
        node(row, "title", f"合成标题{i}", f"[110,{top+10}][310,{top+45}]")
        node(row, "upuser", f"作者{i}", f"[110,{top+60}][310,{top+90}]")
        node(row, "duration", "1:00", f"[50,{top+100}][100,{top+130}]")
    if ended:
        top = 90 + count * 170
        node(grid, "footer", "没有更多了", f"[0,{top}][320,{top+40}]")
    return ET.tostring(root, encoding="unicode")


class CaptureFlowTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".local/android/goal-tests"
        base.mkdir(exist_ok=True, parents=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (320, 800), (32, 80, 180)).save(self.source)

    def run_capture(self, xml, top_n=3, **kwargs):
        source = self.source
        class FakeAdb:
            def __init__(self, deadline, cancel_file=None):
                self.deadline, self.cancel_file, self.serial = deadline, cancel_file, "fixture"
            def discover(self, serial):
                return dict(serial=self.serial)
            def installed(self, package):
                return True
            def screenshot(self, path):
                with path.open("xb") as stream:
                    stream.write(source.read_bytes())
            def run(self, *args):
                raise AssertionError("不应继续滚动")
        with patch("opinion_capture.android.AdbClient", FakeAdb), patch("opinion_capture.android._ui_call", return_value=xml):
            return capture(dict(platform="bilibili", keyword="测试", capture_kind="results", top_n=top_n,
                                timeout_seconds=1800, serial="fixture", output_dir=str(self.root), **kwargs))

    def test_verified_primary_and_source_contract(self):
        result = self.run_capture(document())
        self.assertEqual(result["status"], "success", result["reason"])
        self.assertTrue(result["rank_verified"])
        self.assertEqual(result["observed_count"], 3)
        self.assertEqual(len(result["source_artifact_paths"]), 3)
        self.assertIn("derived", result["primary_artifact_path"])
        self.assertNotIn(result["primary_artifact_path"], result["source_artifact_paths"])
        raw = next(p for p in result["source_artifact_paths"] if p.endswith(".png"))
        self.assertEqual(Path(raw).read_bytes(), self.source.read_bytes())
        self.assertTrue(all(i["source_mappings"] and i["cover_sha256"] for i in result["items"]))
        with Image.open(result["primary_artifact_path"]) as image:
            self.assertEqual(image.height, 590)

    def test_explicit_insufficient_success_and_end_in_output(self):
        result = self.run_capture(document(2, ended=True))
        self.assertEqual(result["status"], "success", result["reason"])
        self.assertEqual(result["outcome"], "insufficient_results")
        self.assertEqual(result["observed_count"], 2)
        with Image.open(result["primary_artifact_path"]) as image:
            self.assertEqual(image.height, 470)

    def test_explicit_zero_results_not_ambiguous_empty(self):
        result = self.run_capture(document(0, ended=True))
        self.assertEqual(result["status"], "success", result["reason"])
        self.assertEqual(result["observed_count"], 0)
        self.assertEqual(result["outcome"], "insufficient_results")

    def test_other_app_is_not_target_login(self):
        xml = '<hierarchy><node package="com.xingin.xhs" text="手机号登录" bounds="[0,0][320,800]"/></hierarchy>'
        result = self.run_capture(xml)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "foreground_app_changed")
        self.assertIsNone(result["primary_artifact_path"])

    def test_known_partial_card_has_pending_boundary(self):
        xml = document().replace('[0,430][320,590]', '[0,430][320,800]')
        page = parse_page(xml, "bilibili", "results", "测试")
        self.assertFalse(page["items"][-1]["complete"])
        self.assertTrue(any(r["reason"] == "incomplete_content_card" for r in page["unknown"]))


if __name__ == "__main__":
    unittest.main()
