"""可选的真实资料派生验收；显式设置 EXPORT_VISUAL_TESTS=1 才读取 docs 图片。

逐页核对全部成员像素和源文件哈希，并永久保留独立运行目录供人工视觉检查。
这里的资料序号仅为展示标签，不推断图片所属关键词或 App 采集正确性。
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid

from PIL import Image, ImageChops

from opinion_capture.imaging import compose_grid, compose_pages

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("EXPORT_VISUAL_TESTS") == "1", "显式启用真实资料派生视觉验证")
class ExportVisualTests(unittest.TestCase):
    """有完整 28 张冻结序列，也包含两平台长图和另一平台下拉词。"""

    def test_real_documents_pagination_and_source_integrity(self):
        run = ROOT / ".local/export-tests" / ("visual-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        run.mkdir(parents=True)
        samples = [
            ("bilibili-suggestions-28", "20点B站下拉/20点B站下拉", 28, "grid", 4, 24000000, 5400),
            ("bilibili-long", "20点B站TOP20", 3, "grid", 3, 40000000, 18000),
            ("xiaohongshu-long", "21点小红书TOP20/21点小红书TOP20", 3, "horizontal", 3, 40000000, 18000),
            ("xiaohongshu-suggestions", "21点小红书下拉词/21点小红书下拉词", 3, "horizontal", 3, 12000000, 5400),
        ]
        report = {"source_root": str(ROOT / "docs"), "run_dir": str(run), "groups": [],
                  "note": "资料展示编号不是已核实业务关键词；不代表平台新采集或滚动长图实测通过。"}
        for name, folder, count, layout, columns, max_pixels, max_height in samples:
            paths = sorted((ROOT / "docs" / folder).glob("*.jpg"))[:count]
            self.assertEqual(len(paths), count)
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
            members = [{"keyword": f"参考资料 {i + 1:02d}", "status": "success", "source_filename": path.name}
                       for i, path in enumerate(paths)]
            result = compose_pages(paths, run / name, members=members, layout=layout, columns=columns,
                                   cell_width=1080, max_height=max_height, max_pixels=max_pixels)
            self.assertEqual([p["index"] for page in result["pages"] for p in page["placements"]], list(range(count)))
            for page in result["pages"]:
                self.assertLessEqual(page["height"], max_height)
                self.assertLessEqual(page["pixels"], max_pixels)
                with Image.open(page["path"]) as canvas:
                    for placement in page["placements"]:
                        x, y, w, h = (placement[k] for k in ("x", "y", "width", "height"))
                        with Image.open(placement["path"]) as source:
                            with source.convert("RGB") as rgb, rgb.resize((w, h), Image.Resampling.LANCZOS) as expected:
                                with canvas.crop((x, y, x + w, y + h)) as actual, ImageChops.difference(expected, actual) as diff:
                                    self.assertIsNone(diff.getbbox(), "派生图片发生遮挡或非预期裁切")
                    self.assertGreaterEqual(min(p["y"] - p["card_box"][1] for p in page["placements"]), 1)
            after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
            self.assertEqual(before, after)
            report["groups"].append({"name": name, "source_count": count, "source_hashes": before, "result": result})
        first_path = Path(report["groups"][0]["result"]["pages"][0]["path"])
        overview = compose_grid([page["path"] for page in report["groups"][0]["result"]["pages"]],
                                run / "all-28-overview.png", columns=2, cell_width=900)
        report["overview"] = overview
        with Image.open(first_path) as image:
            image.crop((0, 0, min(2224, image.width), 900)).save(run / "labels-and-original-text.png")
        example = report["groups"][0]["result"]["pages"][0]["placements"][0]
        state_demo = compose_pages([example["path"], None, example["path"]], run / "failure-markers",
                                  members=[{"keyword": "A1", "status": "success"},
                                           {"keyword": "A10", "status": "failed", "reason": "模拟缺失，仅验证版式"},
                                           {"keyword": "a1", "status": "partial"}], cell_width=540, columns=3)
        report["failure_markers"] = state_demo
        report["verified"] = {"source_hashes_unchanged": True, "all_member_pixels_equal": True,
                              "all_order_indexes_present": True, "visual_review": "pending_human_or_agent_image_inspection"}
        report_path = run / "verification.json"
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(f"REAL_VISUAL_REPORT={report_path}")


if __name__ == "__main__":
    unittest.main()
