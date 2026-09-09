"""图片模块的确定性合成测试；不代表 App 采集或滚动实测。"""

import hashlib
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opinion_capture.imaging import compose_grid, compose_pages, stitch_vertical, ImagingError, OverlapError


class ImagingTests(unittest.TestCase):
    """在指定本地临时目录内验证布局和连续性失败关闭。"""

    def setUp(self):
        base = ROOT / ".local/export-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, name, image):
        """保存测试输入并返回路径。"""
        path = self.root / name
        image.save(path)
        return path

    def frames(self):
        """构造三张确定性连续帧，两处重叠均为 61 像素。"""
        rng = random.Random(20260908)
        base = Image.frombytes("RGB", (72, 301), rng.randbytes(72 * 301 * 3))
        paths = [self.save(f"frame{i}.png", base.crop((0, y, 72, y + 141)))
                 for i, y in enumerate((0, 80, 160))]
        return base, paths

    def test_grid_size_order_whitespace(self):
        a = self.save("red.png", Image.new("RGB", (40, 80), "red"))
        b = self.save("blue.png", Image.new("RGB", (80, 40), "blue"))
        c = self.save("green.png", Image.new("RGB", (40, 40), "green"))
        result = compose_grid([b, a, c], self.root / "grid.png", columns=2, cell_width=40, gap=5)
        self.assertEqual((result["width"], result["height"]), (95, 135))
        self.assertEqual(result["inputs"], [str(p) for p in [b, a, c]])
        with Image.open(result["path"]) as im:
            self.assertEqual(im.getpixel((5, 5)), (0, 0, 255))
            self.assertEqual(im.getpixel((50, 5)), (255, 0, 0))
            self.assertEqual(im.getpixel((5, 90)), (0, 128, 0))
            self.assertEqual(im.getpixel((5, 30)), (255, 255, 255))
        self.assertEqual(result["placements"][0]["height"], 20)

    def test_horizontal_and_small_image(self):
        p = self.save("small.png", Image.new("RGB", (10, 20), "red"))
        result = compose_grid([p, p], self.root / "row.png", columns=9, cell_width=40, gap=0)
        self.assertEqual((result["width"], result["height"]), (80, 20))
        self.assertEqual(result["placements"][0]["width"], 10)

    def test_no_overwrite_and_hashes(self):
        _, paths = self.frames()
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        for function in (compose_grid, stitch_vertical):
            with self.assertRaises(FileExistsError):
                function(paths, paths[0])
        output = self.root / "exists.png"
        output.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            compose_grid(paths, output)
        with self.assertRaises(FileExistsError):
            stitch_vertical(paths, output)
        self.assertEqual(output.read_bytes(), b"keep")
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_pixel_budgets(self):
        _, paths = self.frames()
        for function in (compose_grid, stitch_vertical):
            for kwargs in ({"max_pixels": 100}, {"max_input_pixels": 100}):
                with self.assertRaises(ImagingError):
                    function(paths, self.root / "budget.png", **kwargs)
        with self.assertRaises(ImagingError):
            stitch_vertical(paths, self.root / "budget.png", max_pixels=72 * 200)
        self.assertFalse((self.root / "budget.png").exists())

    def test_stitch_exact_pixels(self):
        base, paths = self.frames()
        result = stitch_vertical(paths, self.root / "stitched.png")
        self.assertEqual([m["overlap"] for m in result["overlaps"]], [61, 61])
        with Image.open(result["path"]) as im:
            self.assertEqual(im.size, base.size)
            self.assertEqual(im.tobytes(), base.tobytes())

    def test_content_crop(self):
        base, paths = self.frames()
        framed = []
        for i, path in enumerate(paths):
            im = Image.new("RGB", (72, 161), "red")
            with Image.open(path) as source:
                im.paste(source, (0, 10))
            framed.append(self.save(f"bar{i}.png", im))
        result = stitch_vertical(framed, self.root / "crop.png", content_top=10, content_bottom=151)
        with Image.open(result["path"]) as im:
            self.assertEqual(im.tobytes(), base.tobytes())

    def test_duplicate_blank_nonoverlap_and_reverse(self):
        _, paths = self.frames()
        blank = self.save("blank.png", Image.new("RGB", (72, 141), "white"))
        for inputs in ([paths[0], paths[0]], [blank, blank], [paths[0], paths[2]], list(reversed(paths))):
            with self.subTest(inputs=inputs), self.assertRaises(OverlapError):
                stitch_vertical(inputs, self.root / "bad.png")
            self.assertFalse((self.root / "bad.png").exists())

    def test_repeated_pattern_ambiguity(self):
        rng = random.Random(4)
        tile = Image.frombytes("RGB", (72, 12), rng.randbytes(72 * 12 * 3))
        base = Image.new("RGB", (72, 300))
        for y in range(0, 300, 12):
            base.paste(tile, (0, y))
        paths = [self.save(f"period{i}.png", base.crop((0, y, 72, y + 141))) for i, y in enumerate((0, 53))]
        with self.assertRaisesRegex(OverlapError, "歧义"):
            stitch_vertical(paths, self.root / "period.png")

    def test_low_information_overlap(self):
        _, paths = self.frames()
        images = []
        for i, path in enumerate(paths[:2]):
            with Image.open(path) as source:
                im = source.copy()
            im.paste("white", (0, 81, 72, 141) if i == 0 else (0, 0, 72, 60))
            images.append(self.save(f"empty{i}.png", im))
        with self.assertRaises(OverlapError):
            stitch_vertical(images, self.root / "empty.png")

    def test_invalid_parameters(self):
        _, paths = self.frames()
        for kwargs in ({"columns": 0}, {"cell_width": 0}, {"gap": -1}, {"columns": True}):
            with self.assertRaises(ImagingError):
                compose_grid(paths, self.root / "invalid.png", **kwargs)
        for kwargs in ({"content_top": 150}, {"min_overlap": 0}, {"max_error": float("nan")}, {"min_margin": 0}):
            with self.assertRaises(ImagingError):
                stitch_vertical(paths, self.root / "invalid.png", **kwargs)
        narrow = self.save("narrow.png", Image.new("RGB", (71, 141), "red"))
        with self.assertRaises(ImagingError):
            stitch_vertical([paths[0], narrow], self.root / "invalid.png")
        with self.assertRaises(ImagingError):
            compose_grid([], self.root / "invalid.png")

    def test_rgb_review_rejects_same_grayscale(self):
        """灰度相同而颜色不同的帧须在原分辨率 RGB 复核阶段报错。"""
        rng = random.Random(16)
        values = [rng.randrange(40, 200) for _ in range(72 * 221)]
        base = Image.frombytes("RGB", (72, 221), bytes(c for v in values for c in (v, v, v)))
        shifted = Image.frombytes("RGB", (72, 221), bytes(c for v in values for c in (v + 20, v - 10, v)))
        self.assertEqual(base.convert("L").tobytes(), shifted.convert("L").tobytes())
        paths = [self.save("color-a.png", base.crop((0, 0, 72, 141))),
                 self.save("color-b.png", shifted.crop((0, 80, 72, 221)))]
        with self.assertRaisesRegex(OverlapError, "RGB"):
            stitch_vertical(paths, self.root / "color-bad.png")
        self.assertFalse((self.root / "color-bad.png").exists())

    def test_rgba_white_background(self):
        p = self.save("alpha.png", Image.new("RGBA", (10, 10), (255, 0, 0, 0)))
        result = compose_grid([p], self.root / "alpha-grid.png", cell_width=10, gap=0)
        with Image.open(result["path"]) as im:
            self.assertEqual(im.getpixel((0, 0)), (255, 255, 255))

    def test_output_alias_and_extension(self):
        import os
        _, paths = self.frames()
        alias = self.root / "hardlink.png"
        os.link(paths[0], alias)
        for function in (compose_grid, stitch_vertical):
            with self.assertRaises(FileExistsError):
                function(paths, alias)
            with self.assertRaises(ImagingError):
                function(paths, self.root / "wrong.jpg")

    def test_pages_height_budget_preserves_all_members_and_order(self):
        paths = [self.save(f"item-{i}.png", Image.new("RGB", (120, 100), (i * 25, 0, 0))) for i in range(7)]
        result = compose_pages(paths, self.root / "pages", columns=2, cell_width=120, gap=5,
                               max_height=115, max_pixels=30000)
        self.assertEqual(result["page_count"], 1)
        items = [p for page in result["pages"] for p in page["placements"]]
        self.assertEqual([p["index"] for p in items], list(range(7)))
        for page in result["pages"]:
            self.assertEqual(page["height"], 425)
            self.assertGreater(page["pixels"], 30000)
            with Image.open(page["path"]) as im:
                for item in page["placements"]:
                    self.assertEqual(im.getpixel((item["x"], item["y"])), (item["index"] * 25, 0, 0))
                    self.assertEqual((item["width"], item["height"]), (120, 100))

    def test_horizontal_ignores_old_limit_without_wrapping(self):
        p = self.save("tile.png", Image.new("RGB", (100, 100), "red"))
        result = compose_pages([p] * 5, self.root / "horizontal", layout="horizontal", cell_width=100,
                               gap=0, max_pixels=20000)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual([len(page["placements"]) for page in result["pages"]], [5])
        self.assertTrue(all(p["row"] == 0 for page in result["pages"] for p in page["placements"]))

    def test_grid_keeps_configured_columns_despite_old_limit(self):
        p = self.save("tile.png", Image.new("RGB", (100, 100), "red"))
        result = compose_pages([p] * 5, self.root / "grid", columns=4, cell_width=100, gap=0, max_pixels=10000)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["pages"][0]["columns"], 4)

    def test_missing_and_partial_members_explicit_no_image_crop(self):
        p = self.save("text.png", Image.new("RGB", (600, 200), "blue"))
        members = [{"keyword": "A1", "status": "success"}, {"keyword": "A10", "status": "failed", "reason": "timeout"},
                   {"keyword": "a1", "status": "partial"}, {"keyword": "A/1", "status": "blocked"}]
        frozen = [dict(m) for m in members]
        result = compose_pages([p, None, p, p], self.root / "states", members=members,
                               cell_width=300, columns=2, max_height=1000)
        items = [p for page in result["pages"] for p in page["placements"]]
        self.assertEqual([p["keyword"] for p in items], ["A1", "a1"])
        self.assertEqual([p["index"] for p in items], [0, 2])
        self.assertEqual([p["keyword"] for p in result["omitted_members"]], ["A10", "A/1"])
        self.assertTrue(all(not p["placeholder"] and p["label_box"] is None for p in items))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(members, frozen)
        with Image.open(result["pages"][0]["path"]) as im:
            item = items[0]
            self.assertEqual(item["y"], item["card_box"][1])
            self.assertEqual((item["width"], item["height"]), (300, 100))
            self.assertEqual(im.crop((item["x"], item["y"], item["x"] + 300, item["y"] + 100)).getextrema(),
                             ((0, 0), (0, 0), (255, 255)))

    def test_all_failed_metadata_without_placeholder_images(self):
        result = compose_pages([None, None], self.root / "missing", members=[
            {"keyword": "长关键词" * 20, "status": "failed"}, {"keyword": "A10", "status": "blocked"}],
            cell_width=180, columns=1, max_height=2000)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["page_count"], 0)
        self.assertEqual(len(result["omitted_members"]), 2)
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(len(result["members"]), 2)

    def test_single_tall_cell_is_not_sliced_by_old_limit(self):
        p = self.save("tall.png", Image.new("RGB", (100, 501), "red"))
        result = compose_pages([p], self.root / "tall-out", cell_width=100, max_height=500, gap=0)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["pages"][0]["height"], 501)

    def test_pages_path_factory_and_index_template(self):
        p = self.save("tile.png", Image.new("RGB", (80, 80), "red"))
        calls = []
        def output(part):
            calls.append(part)
            return self.root / "named" / f"任意文字-{part}.png"
        result = compose_pages([p] * 3, self.root / "named", layout="horizontal", cell_width=80,
                               max_pixels=6400, gap=0, output_path_factory=output)
        self.assertEqual(calls, [1])
        self.assertEqual([p["part"] for p in result["pages"]], [1])
        r = compose_pages([p], self.root / "template", cell_width=80, filename_template="第{index:03d}页.png")
        self.assertEqual(Path(r["pages"][0]["path"]).name, "第001页.png")

    def test_all_targets_checked_before_any_page_is_saved(self):
        p = self.save("tile.png", Image.new("RGB", (100, 100), "red"))
        out = self.root / "precheck"
        for factory in (lambda part: p, lambda part: self.root / "outside.png", lambda part: out / "invalid.jpg"):
            with self.assertRaises((FileExistsError, ImagingError)):
                compose_pages([p, p], out, cell_width=100, gap=0, max_pixels=10000, output_path_factory=factory)
            self.assertFalse(out.exists())
        out.mkdir()
        (out / "page_01.png").write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            compose_pages([p, p], out, cell_width=100, gap=0, max_pixels=10000)
        self.assertFalse((out / "page_02.png").exists())
        self.assertEqual((out / "page_01.png").read_bytes(), b"keep")

    def test_page_invalid_arguments(self):
        p = self.save("tile.png", Image.new("RGB", (10, 10), "red"))
        for kwargs in ({"columns": True}, {"cell_width": 0}, {"layout": "vertical"}, {"members": []},
                       {"filename_template": "../bad.png"}, {"filename_template": "{part.__class__}.png"},
                       {"filename_template": "bad.jpg"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ImagingError):
                compose_pages([p], self.root / "invalid-pages", **kwargs)

    def test_save_failure_never_exposes_partial_png(self):
        p = self.save("tile.png", Image.new("RGB", (10, 10), "red"))
        destination = self.root / "output.png"
        def fail(image, stream, **kwargs):
            stream.write(b"incomplete-png")
            self.assertFalse(destination.exists())
            raise OSError("encoder failed")
        with patch("PIL.Image.Image.save", new=fail):
            with self.assertRaises(OSError):
                compose_grid([p], destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_png_verify_failure_has_no_final_artifact(self):
        p = self.save("tile.png", Image.new("RGB", (10, 10), "red"))
        destination = self.root / "output.png"
        def malformed(image, stream, **kwargs):
            stream.write(b"not a png")
        with patch("PIL.Image.Image.save", new=malformed):
            with self.assertRaises(OSError):
                compose_grid([p], destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_mixed_sizes_single_grid_invariants(self):
        rng = random.Random(20260908)
        paths = [self.save(f"mixed{i}.png", Image.new("RGB", (rng.randint(20, 100), rng.randint(20, 90)),
                                                          (i * 20, 40, 100))) for i in range(10)]
        for columns in (1, 2, 4):
            result = compose_pages(paths, self.root / f"mixed-out-{columns}", columns=columns, cell_width=80,
                                   gap=3, max_height=180, max_pixels=20000)
            self.assertEqual([p["index"] for page in result["pages"] for p in page["placements"]], list(range(10)))
            self.assertEqual(result["page_count"], 1)
            for page in result["pages"]:
                with Image.open(page["path"]) as canvas:
                    for p in page["placements"]:
                        self.assertLessEqual(p["width"], p["source_size"][0])
                        self.assertLessEqual(p["height"], p["source_size"][1])
                        self.assertLessEqual(p["x"] + p["width"], canvas.width)
                        self.assertLessEqual(p["y"] + p["height"], canvas.height)
                        self.assertEqual(canvas.getpixel((p["x"] + p["width"] - 1, p["y"] + p["height"] - 1)),
                                         (p["index"] * 20, 40, 100))

    def test_single_image_end_failure_has_no_partial_artifact(self):
        from opinion_capture import imaging
        p = self.save("tile.png", Image.new("RGB", (100, 100), "red"))
        out = self.root / "later-failure"
        original = imaging._png_chunk
        def interrupted(stream, kind, payload):
            if kind == b"IEND":
                raise OSError("final chunk write failed")
            return original(stream, kind, payload)
        with patch("opinion_capture.imaging._png_chunk", side_effect=interrupted):
            with self.assertRaises(imaging.PageCompositionError):
                compose_pages([p, p], out, cell_width=100, gap=0, max_pixels=10000)
        self.assertEqual(list(out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
