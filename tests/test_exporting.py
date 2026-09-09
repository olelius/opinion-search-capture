"""不可变发布：同名竞争、无硬链接 Windows 兼容、故障清理与部分文件隔离。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from opinion_capture.exporting import copy_immutable, publish_immutable

ROOT = Path(__file__).resolve().parents[1]


class ImmutableExportTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".local/export-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        self.source.write_bytes(b"immutable-evidence" * 10000)
        self.target = self.root / "export" / "target.png"

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_copy_hash_and_no_overwrite(self):
        result = copy_immutable(self.source, self.target)
        self.assertEqual(result["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(result["bytes"], self.source.stat().st_size)
        with self.assertRaises(FileExistsError):
            copy_immutable(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    @unittest.skipUnless(os.name == "nt", "验证 Windows 原生不替换重命名")
    def test_windows_without_hardlink_support(self):
        with patch("opinion_capture.exporting.os.link", side_effect=OSError("hardlinks unsupported")):
            result = copy_immutable(self.source, self.target)
        self.assertEqual(result["publication"], "windows_rename_noreplace")
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())

    def test_copy_failure_has_no_final_partial_file(self):
        def interrupted(source, output):
            self.assertFalse(self.target.exists())
            output.write(b"PARTIAL")
            self.assertFalse(self.target.exists())
            raise OSError("copy interrupted")
        with patch("opinion_capture.exporting.shutil.copyfileobj", side_effect=interrupted):
            with self.assertRaises(OSError):
                copy_immutable(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_fsync_failure_has_no_final_file(self):
        with patch("opinion_capture.exporting.os.fsync", side_effect=OSError("sync interrupted")):
            with self.assertRaises(OSError):
                copy_immutable(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_posix_no_weak_fallback(self):
        with patch("opinion_capture.exporting._WINDOWS", False), \
             patch("opinion_capture.exporting.os.link", side_effect=OSError("unsupported")):
            with self.assertRaisesRegex(OSError, "未完成排他硬链接发布"):
                copy_immutable(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_concurrent_publish_only_one_wins(self):
        def attempt(_):
            try:
                return copy_immutable(self.source, self.target)
            except FileExistsError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(24)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.source.read_bytes(), self.target.read_bytes())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_late_competitor_not_overwritten(self):
        def competitor(staged, destination):
            Path(destination).write_bytes(b"winner")
            return publish_immutable(staged, destination)
        with patch("opinion_capture.exporting.publish_immutable", side_effect=competitor):
            with self.assertRaises(FileExistsError):
                copy_immutable(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"winner")
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_staged_path_must_share_destination_directory(self):
        with self.assertRaises(ValueError):
            publish_immutable(self.source, self.target)
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
