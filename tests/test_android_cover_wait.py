"""封面等待的新增调用边界检查：不运行ADB、浏览器或已有回归集。"""

from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from opinion_capture.android import _collect
from opinion_capture.android_results import EvidenceError


class CoverWaitTests(unittest.TestCase):
    def test_unverified_cover_waits_bounded_without_ranking_or_scrolling(self):
        adb = SimpleNamespace(deadline=time.monotonic()+5, cancel_file=None)
        result = dict(requested_top_n=20,capture_kind="results",frames=[],items=[])
        def snapshot(*args):
            frame = dict(stable=True, png_path="not-read.png", page={"platform":"xiaohongshu",
                "items":[dict(identity="pending",complete=True,cover_bounds=[0,0,100,100])]})
            result["frames"].append(frame)
            return frame
        with patch("opinion_capture.android._snapshot",side_effect=snapshot) as snapshots, \
             patch("opinion_capture.android_image_evidence.assess_cover",return_value={"status":"blank_or_unverified"}), \
             patch("opinion_capture.android.time.sleep"), \
             patch("opinion_capture.android._swipe_forward") as swipe, \
             patch("opinion_capture.android_results.RankLedger.add") as add:
            with self.assertRaisesRegex(EvidenceError,"cover_loading_not_confirmed"):
                _collect(adb,Path("unused"),result,{"max_frames":80})
        self.assertEqual(snapshots.call_count,3)
        swipe.assert_not_called()
        add.assert_not_called()
        self.assertEqual(result["observed_count"],0)
        self.assertEqual(len(result["frames"][-1]["cover_readiness"]),1)


if __name__ == "__main__": unittest.main()
