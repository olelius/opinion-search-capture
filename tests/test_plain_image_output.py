"""去除程序绘制信息的定点：原像素、分页映射与缺图元数据。"""
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageChops
from opinion_capture.imaging import compose_pages
from opinion_capture.web_results_bilibili import compose_primary, png_details


class PlainImageOutputTests(unittest.TestCase):
    def test_bilibili_pages_have_no_added_rows_and_keep_source_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pages = []
            for index, color in enumerate(("red", "blue"), 1):
                path = root / f"source-{index}.png"
                with Image.new("RGB", (20, 30), color) as image:
                    image.putpixel((1, 1), (10, 20, 30))
                    image.save(path)
                pages.append({"page_index": index, "source": png_details(path),
                              "crop": {"box": [0, 0, 20, 30]}, "occurrences": []})
            original_hashes = [p["source"]["sha256"] for p in pages]
            result = compose_primary(pages, root / "plain.png", 20, lambda: None)
            self.assertFalse(result["added_annotations"])
            self.assertEqual(result["size"], [20, 60])
            with Image.open(result["path"]) as final:
                for index, page in enumerate(pages):
                    segment = result["segments"][index]
                    self.assertEqual(segment["destination_box"], [0, index*30, 20, (index+1)*30])
                    with Image.open(page["source"]["path"]) as original:
                        self.assertIsNone(ImageChops.difference(final.crop(segment["destination_box"]), original).getbbox())
            self.assertEqual(original_hashes, [hashlib.sha256(Path(p["source"]["path"]).read_bytes()).hexdigest() for p in pages])

    def test_keyword_and_partial_status_are_metadata_not_image_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "source.png"
            Image.new("RGB", (20, 30), "green").save(path)
            result = compose_pages([path, path], root / "pages", cell_width=20, columns=2, gap=0,
                members=[{"keyword":"A1","status":"success"},{"keyword":"A10","status":"partial"}])
            page = result["pages"][0]
            self.assertEqual((page["width"], page["height"]), (40,30))
            self.assertEqual(result["status"], "partial")
            self.assertEqual([m["keyword"] for m in page["members"]], ["A1","A10"])
            self.assertTrue(all(p["y"]==0 and p["label_box"] is None for p in page["placements"]))
            with Image.open(page["path"]) as image, Image.open(path) as original:
                for x in (0,20):
                    self.assertIsNone(ImageChops.difference(image.crop((x,0,x+20,30)),original).getbbox())


if __name__ == "__main__":
    unittest.main()
