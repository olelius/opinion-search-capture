"""单张汇总、按列换行与流式PNG；不访问平台、不执行便携构建。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from opinion_capture import imaging
from opinion_capture.config import Plan
from opinion_capture.runner import run_plan
from tests.test_runner import fixture_collector


class SingleCompositeTests(unittest.TestCase):
    def test_ten_keywords_wrap_four_four_two_in_one_png_despite_old_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = []
            for index in range(10):
                path = root / f'{index}.png'
                Image.new('RGB', (20, 30 + index), (index * 20, 40, 90)).save(path)
                sources.append(path)
            result = imaging.compose_pages(sources, root / 'out', columns=4, cell_width=20, gap=2,
                members=[dict(keyword=f'A{i+1}', status='success') for i in range(10)],
                max_pixels=1, max_height=1, max_input_pixels=1)
            self.assertEqual(result['page_count'], 1)
            page = result['pages'][0]
            self.assertEqual([sum(p['row'] == row for p in page['placements']) for row in range(3)], [4, 4, 2])
            self.assertEqual((page['width'], page['height']), (90, 117))
            self.assertEqual(result['rendered_order'], list(range(10)))
            self.assertEqual(len(list((root / 'out').glob('*.png'))), 1)
            with Image.open(page['path']) as image:
                for placement in page['placements']:
                    box = (placement['x'], placement['y'], placement['x'] + placement['width'], placement['y'] + placement['height'])
                    with Image.open(sources[placement['index']]) as source:
                        self.assertEqual(image.crop(box).tobytes(), source.tobytes())
            self.assertFalse(list((root / 'out').glob('.compose-*')))

    def test_tall_input_and_output_ignore_pillow_global_area_guard_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'tall.png'
            Image.new('RGB', (64, 25001), 'blue').save(path)
            with patch.object(Image, 'MAX_IMAGE_PIXELS', 100):
                result = imaging.compose_pages([path], root / 'out', cell_width=64, gap=0)
                self.assertEqual(Image.MAX_IMAGE_PIXELS, 100)
                with PngImagePlugin.PngImageFile(result['pages'][0]['path']) as image:
                    self.assertEqual(image.size, (64, 25001))
                    self.assertEqual(image.getpixel((63, 25000)), (0, 0, 255))

    def test_stream_error_publishes_no_partial_file_and_cleans_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'source.png'
            Image.new('RGB', (64, 100), 'red').save(path)
            original = imaging._png_chunk
            def fail_on_data(stream, kind, payload):
                if kind == b'IDAT':
                    raise OSError('disk full fixture')
                return original(stream, kind, payload)
            with patch.object(imaging, '_png_chunk', side_effect=fail_on_data):
                with self.assertRaisesRegex(imaging.PageCompositionError, 'disk full fixture'):
                    imaging.compose_pages([path], root / 'out', cell_width=64)
            self.assertEqual(list((root / 'out').iterdir()), [])

    def test_runner_exports_all_keywords_as_one_composite(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Plan(output_root=directory, keywords=[f'A{i}' for i in range(10)],
                        platforms=['bilibili'], clients=['WEB'], capture_kinds=['suggestions'],
                        composite_columns=4, composite_cell_width=64,
                        composite_max_pixels=100000, composite_max_height=256)
            report = run_plan(plan, collector=fixture_collector)
            self.assertEqual(report['status'], 'success')
            self.assertEqual(len(report['composites']), 1)
            group = report['composites'][0]
            self.assertEqual(group['keywords'], plan.keywords)
            self.assertEqual(len(group['placements']), 10)
            self.assertGreater(group['height'], 256)
            self.assertEqual(group['part'], 1)


if __name__ == '__main__':
    unittest.main()
