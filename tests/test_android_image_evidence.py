import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from opinion_capture.android_image_evidence import assess_cover


EVIDENCE = Path(__file__).parents[1] / ".local/completion/app-final-20260908-231446-945f8e/xiaohongshu/20260908-231446-ffda76731a/source"


class AndroidImageEvidenceTests(unittest.TestCase):
    def test_saved_013_content_and_021_placeholder(self):
        good = assess_cover(EVIDENCE / "frame-013.png", [544, 747, 1072, 1451])
        blank = assess_cover(EVIDENCE / "frame-021.png", [544, 1028, 1072, 1732])
        self.assertEqual(good["status"], "content_visible")
        self.assertEqual(blank["status"], "blank_or_unverified")
        self.assertTrue(blank["retry_recommended"])
        self.assertNotEqual(good["cover_sha256"], blank["cover_sha256"])

    def test_synthetic_three_to_five_boundary_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cases = {}
            solid = Image.new("RGB", (180, 180), (248, 248, 248))
            gradient = Image.new("RGB", (180, 180))
            px = gradient.load()
            for y in range(180):
                for x in range(180):
                    value = 238 + int(12 * x / 179)
                    px[x, y] = (value, value, value)
            text = Image.new("RGB", (180, 180), "white")
            ImageDraw.Draw(text).text((35, 70), "A", fill=(25, 25, 25), stroke_width=1)
            texture = Image.new("RGB", (180, 180), "white")
            draw = ImageDraw.Draw(texture)
            for x in range(0, 180, 7):
                draw.line((x, 0, x, 179), fill=(190, 190, 190), width=1)
            photo = Image.new("RGB", (180, 180), (28, 95, 145))
            ImageDraw.Draw(photo).ellipse((25, 20, 155, 150), fill=(230, 170, 60))
            for name, image in (("solid", solid), ("gradient", gradient), ("text", text), ("texture", texture), ("photo", photo)):
                path = root / f"{name}.png"
                image.save(path)
                cases[name] = assess_cover(path, (0, 0, 180, 180))
            self.assertEqual(cases["solid"]["status"], "blank_or_unverified")
            self.assertEqual(cases["gradient"]["status"], "blank_or_unverified")
            self.assertEqual(cases["text"]["status"], "blank_or_unverified")
            self.assertEqual(cases["texture"]["status"], "content_visible")
            self.assertEqual(cases["photo"]["status"], "content_visible")

    def test_hash_is_full_crop_bytes_and_partial_cover_does_not_retry(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cover.png"
            image = Image.new("RGB", (20, 20), "white")
            ImageDraw.Draw(image).rectangle((3, 3, 16, 16), fill="black")
            image.save(path)
            result = assess_cover(path, (0, 0, 20, 20), fully_visible=False)
            expected = hashlib.sha256(image.tobytes()).hexdigest()
            self.assertEqual(result["cover_sha256"], expected)
            self.assertFalse(result["retry_recommended"])


if __name__ == "__main__":
    unittest.main()
