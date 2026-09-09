"""封面等待的新增调用边界检查：不运行ADB、浏览器或已有回归集。"""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from opinion_capture.android import AndroidLoadingTimeout, _collect


class CoverWaitTests(unittest.TestCase):
    def test_unverified_cover_waits_bounded_without_ranking_or_scrolling(self):
        clock = SimpleNamespace(now=0.0)
        def sleep(seconds):
            clock.now += seconds

        adb = SimpleNamespace(deadline=180, cancel_file=None)
        result = dict(platform="xiaohongshu",keyword="测试",requested_top_n=20,capture_kind="results",frames=[],items=[])
        page = dict(platform="xiaohongshu", viewport=[0,0,320,800], keyword_verified=True,
                    scope_verified=True, recognized=True, excluded=[], unknown=[], end_marker=None,
                    items=[dict(identity="pending",complete=True,bounds=[0,0,100,120],cover_bounds=[0,0,100,100])])
        def snapshot(*args):
            frame = dict(stable=True, png_path="not-read.png", page=page, source_index=len(result["frames"]))
            result["frames"].append(frame)
            return frame
        with patch("opinion_capture.android._snapshot",side_effect=snapshot) as snapshots, \
             patch("opinion_capture.android_image_evidence.assess_cover",return_value={"status":"blank_or_unverified"}), \
             patch("opinion_capture.android.time.monotonic",side_effect=lambda: clock.now), \
             patch("opinion_capture.android.time.sleep",side_effect=sleep), \
             patch("opinion_capture.android._dump",return_value="<hierarchy/>"), \
             patch("opinion_capture.android_results.parse_page",return_value=page), \
             patch("opinion_capture.android._swipe_forward") as swipe, \
             patch("opinion_capture.android_results.RankLedger.add") as add:
            with self.assertRaisesRegex(AndroidLoadingTimeout,"cover_loading_not_confirmed_within_budget"):
                _collect(adb,Path("unused"),result,{"max_frames":80})
        # 预算取代旧“两次等待”；XML不变仍复核封面，但25秒内始终不排名/不滑动。
        self.assertGreater(snapshots.call_count,3)
        self.assertAlmostEqual(clock.now,25)
        self.assertEqual(adb.deadline,180)
        wait = result["loading_wait"]
        self.assertEqual(wait["budget_seconds"],25)
        self.assertAlmostEqual(wait["elapsed_seconds"],25)
        self.assertEqual(wait["episodes"][0]["status"],"timeout")
        self.assertEqual(wait["episodes"][0]["snapshots"],snapshots.call_count - 1)
        swipe.assert_not_called()
        add.assert_not_called()
        self.assertEqual(result["observed_count"],0)
        self.assertEqual(len(result["frames"][-1]["cover_readiness"]),1)


if __name__ == "__main__": unittest.main()
