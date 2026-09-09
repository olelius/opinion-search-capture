"""文档滚动拼接定点：人工固定导航与有坐标纹理的原帧，全程离线。"""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from opinion_capture.web_results_xiaohongshu import analyse_frames
from opinion_capture.web_scrollshot import compose_scrollshot


def _fixture(root):
    """生成三帧：固定 14px 页头、左右侧栏，卡片随 document 坐标真实移动。"""
    with Image.new("RGB", (32, 200)) as world:
        world.putdata([(x*5, y, (x*13 ^ y) % 256) for y in range(200) for x in range(32)])
        frames = []
        for index, offset in enumerate((0, 56, 112)):
            path = root / f"frame-{index}.png"
            with world.crop((0, offset, 32, offset+80)) as image:
                draw = ImageDraw.Draw(image)
                draw.rectangle((0, 0, 31, 13), fill=(255, 0, 200))
                draw.rectangle((0, 0, 7, 79), fill=(0, 90, 180))
                draw.rectangle((28, 0, 31, 79), fill=(10, 100, 190))
                image.save(path)
            identity = "000000000000000000000001"
            y = 20-offset
            note = dict(id=identity, href=f"https://www.xiaohongshu.com/explore/{identity}",
                        title="人工坐标卡", text="人工坐标卡", rect=[8, y, 20, 160],
                        coverage=[max(0, 14-y), min(160, 80-y)],
                        cover_present=True, images_loaded=True)
            state = dict(schema_version=2, feed_count=1, search_tab="综合", results=[note],
                         viewport=dict(width=32, height=80),
                         scroll=dict(host="document", rect=[0, 0, 32, 80], top=offset,
                                     at_top=offset == 0, at_bottom=False, height=200))
            frames.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               dimensions=[32, 80], stable=True, before=deepcopy(state), after=state))
    return frames, analyse_frames(frames, 1)


class DocumentScrollshotTests(unittest.TestCase):
    """核对导航去重、逐卡原像素映射和证据不足时的拒绝发布。"""

    def test_document_seams_preserve_target_pixels_and_first_sidebar_once(self):
        """重叠带接缝避开固定页头；源图完整侧栏只在原视口高度内出现一次。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames, analysis = _fixture(root)
            self.assertTrue(analysis["rank_verified"])
            original = [Path(frame["path"]).read_bytes() for frame in frames]
            saved_frames, saved_analysis = deepcopy(frames), deepcopy(analysis)
            result = compose_scrollshot(frames, analysis, root / "result.png")
            self.assertEqual((result["width"], result["height"]), (32, 180))
            self.assertEqual(result["content_x"], [8, 28])
            self.assertEqual(result["captured_count"], 1)
            self.assertEqual(len(result["document_seams"]), 2)
            for seam in result["document_seams"]:
                low, high = seam["matching_world_band"]
                self.assertLess(low, seam["world_y"])
                self.assertLess(seam["world_y"], high)
                self.assertGreaterEqual(seam["source_y"][1], 14)
            with Image.open(result["path"]) as output, Image.open(frames[0]["path"]) as first:
                self.assertEqual(output.crop((0, 0, 8, 80)).tobytes(), first.crop((0, 0, 8, 80)).tobytes())
                self.assertEqual(output.crop((28, 0, 32, 80)).tobytes(), first.crop((28, 0, 32, 80)).tobytes())
                self.assertEqual(output.crop((8, 0, 28, 14)).tobytes(), first.crop((8, 0, 28, 14)).tobytes())
                self.assertEqual(output.crop((0, 80, 8, 180)).getextrema(), ((255, 255),)*3)
                self.assertEqual(output.crop((28, 80, 32, 180)).getextrema(), ((255, 255),)*3)
                # 整张目标卡逐像素等于世界坐标纹理，包括跨接缝的每一行。
                self.assertEqual(list(output.crop((8, 20, 28, 180)).getdata()),
                                 [(x*5, y, (x*13 ^ y) % 256) for y in range(20, 180) for x in range(8, 28)])
                for placement in result["placements"]:
                    with Image.open(placement["source_path"]) as source:
                        self.assertEqual(output.crop(placement["output_box"]).tobytes(),
                                         source.crop(placement["source_box"]).tobytes())
                card = result["target_coverage"][0]
                self.assertEqual(card["output_box"], [8, 20, 28, 180])
                self.assertEqual(len(card["segments"]), 3)
                filled = 20
                for segment in card["segments"]:
                    self.assertEqual(segment["output_box"][1], filled)
                    filled = segment["output_box"][3]
                    with Image.open(frames[segment["frame_index"]]["path"]) as source:
                        self.assertEqual(output.crop(segment["output_box"]).tobytes(),
                                         source.crop(segment["source_box"]).tobytes())
                self.assertEqual(filled, 180)
            self.assertEqual([Path(frame["path"]).read_bytes() for frame in frames], original)
            self.assertEqual(frames, saved_frames)
            self.assertEqual(analysis, saved_analysis)

    def test_missing_pixel_or_coverage_proof_and_changed_sources_publish_nothing(self):
        """排名成功也须验证接缝、选中卡片覆盖和原帧哈希；失败不发布半图。"""
        cases = (("pixels", "document_scrollshot_no_proven_pixel_seam"),
                 ("coverage", "document_scrollshot_no_proven_pixel_seam"),
                 ("hash", "scrollshot_source_changed"))
        for case, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                frames, analysis = _fixture(root)
                if case == "coverage":
                    # 故意保留 stale success，证明拼接器另行检查实际选中区间。
                    for snapshot in (frames[0]["before"], frames[0]["after"]):
                        snapshot["results"][0]["coverage"][0] = 10
                    analysis["items"][0]["screenshot_refs"][0]["coverage"][0] = 10
                else:
                    source = Path(frames[1]["path"])
                    with Image.open(source) as image:
                        changed = image.copy()
                    with changed:
                        ImageDraw.Draw(changed).rectangle((8, 14, 27, 79), fill=(1, 2, 3))
                        changed.save(source)
                    if case == "pixels":
                        frames[1]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                output = root / "rejected.png"
                with self.assertRaisesRegex(ValueError, reason):
                    compose_scrollshot(frames, analysis, output)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
