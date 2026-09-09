"""独立笔记跨帧纯函数回归；人工快照不等同平台通过。"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opinion_capture import web_xiaohongshu as adapter
from opinion_capture.web_results_xiaohongshu import analyse_frames, fingerprint, interval_covered, note_identity


def card(number, **changes):
    """创建具备严格身份及可见区间的人工笔记快照。"""
    identity = f"{number:024x}"
    return dict(dict(id=identity, href=f"https://www.xiaohongshu.com/explore/{identity}",
                     title=f"笔记{number}", text=f"内容{number}", promoted=False, rect=[0, 0, 200, 300],
                     images_loaded=True, cover_present=True, coverage=[0, 300]), **changes)


def frame(items=None, *, top=True, end=False, stable=True, **changes):
    """人工 PNG 元数据是纯函数输入，不在磁盘伪造真实平台截图。"""
    state = dict(schema_version=2, url="https://www.xiaohongshu.com/search_result?keyword=零跑",
                 keyword="零跑", input_visible=True, results=items or [], search_tab="综合", feed_count=1,
                 viewport={"width": 1440, "height": 1080},
                 scroll=dict(at_top=top, at_bottom=end, height=2000), explicit_end=end,
                 end_text="没有更多了" if end else "", loading=False, **changes)
    return dict(before=deepcopy(state), after=deepcopy(state), stable=stable,
                path="offline-fixture-frame.png", sha256="a"*64, dimensions=[1440, 1080])


class RankTests(unittest.TestCase):
    def test_exact_top_n_has_success_proof(self):
        result = analyse_frames([frame([card(1), card(2), card(3)])], 3)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["rank_verified"])

    def test_promotion_and_top_n_boundary(self):
        result = analyse_frames([frame([card(1), card(2, promoted=True), card(3), card(4)])], 3)
        self.assertEqual(result["observed_count"], 4)
        self.assertEqual([i["rank"] for i in result["items"] if i["counted"]], [1, 2, 3])
        self.assertTrue(result["items"][1]["promoted"])
        self.assertFalse(result["items"][3]["counted"])

    def test_canonical_identity_ignores_tracking(self):
        item = card(1)
        item["href"] += "?xsec_token=local-test&source=test"
        self.assertEqual(note_identity(item), card(1)["id"])

    def test_conflicting_or_foreign_identity(self):
        for item in [card(1, id=card(2)["id"]), card(1, href="https://evil.test/explore/"+card(1)["id"]),
                     card(1, href="https://www.xiaohongshu.com/user/profile/"+card(1)["id"])]:
            self.assertIsNone(note_identity(item))

    def test_deduplicate_note_not_dom_nodes(self):
        result = analyse_frames([frame([card(1), card(1), card(2)])], 2)
        self.assertEqual((result["observed_count"], result["status"]), (2, "success"))

    def test_excluded_aggregates_do_not_count(self):
        result = analyse_frames([frame([card(4, excluded=True), card(1), card(2)])], 2)
        self.assertEqual([i["id"] for i in result["items"]], [card(1)["id"], card(2)["id"]])

    def test_missing_identity_does_not_silently_skip_rank(self):
        result = analyse_frames([frame([card(1), card(4, href=""), card(2)])], 2)
        self.assertFalse(result["rank_verified"])
        self.assertIn("ambiguous_note_identity", result["issues"])

    def test_missing_cover_or_broken_image_is_partial(self):
        for changes in ({"images_loaded": False}, {"cover_present": False}, {"coverage": None}):
            result = analyse_frames([frame([card(1, **changes)])], 1)
            self.assertEqual(result["status"], "partial")

    def test_later_loaded_image_can_supply_evidence(self):
        result = analyse_frames([frame([card(1, images_loaded=False)]), frame([card(1)])], 1)
        self.assertTrue(result["rank_verified"])

    def test_tall_card_multiple_frames_cover_whole_card(self):
        result = analyse_frames([frame([card(1, coverage=[0, 170])]), frame([card(1, coverage=[160, 300])], top=False)], 1)
        self.assertTrue(result["rank_verified"])
        self.assertEqual(len(result["items"][0]["screenshot_refs"]), 2)

    def test_coverage_gap_not_rank_verified(self):
        self.assertFalse(interval_covered([[0, 100], [110, 300]], 300))
        self.assertTrue(interval_covered([[0, 160], [150, 300]], 300))

    def test_recycling_with_identity_overlap(self):
        result = analyse_frames([frame([card(1), card(2)]), frame([card(2), card(3)], top=False)], 3)
        self.assertTrue(result["rank_verified"])
        self.assertEqual([i["rank"] for i in result["items"]], [1, 2, 3])

    def test_recycling_without_overlap_is_partial(self):
        result = analyse_frames([frame([card(1)]), frame([card(2), card(3)], top=False)], 3)
        self.assertIn("unbridged_dom_recycling", result["issues"])

    def test_reordered_cards_is_partial(self):
        result = analyse_frames([frame([card(1), card(2)]), frame([card(2), card(1), card(3)], top=False)], 3)
        self.assertIn("rank_order_changed", result["issues"])

    def test_insertion_before_tail_is_partial(self):
        result = analyse_frames([frame([card(1), card(2)]), frame([card(1), card(3), card(2)], top=False)], 3)
        self.assertIn("rank_insertion_before_tail", result["issues"])

    def test_masonry_order_is_dom_not_y_sort(self):
        result = analyse_frames([frame([card(1, rect=[0, 100, 200, 300]), card(2, rect=[240, 10, 200, 300])])], 2)
        self.assertEqual([i["id"] for i in result["items"]], [card(1)["id"], card(2)["id"]])

    def test_end_requires_two_observations(self):
        first = frame([card(1)], end=True)
        self.assertEqual(analyse_frames([first], 3)["status"], "partial")
        result = analyse_frames([first, deepcopy(first)], 3)
        self.assertEqual((result["status"], result["outcome"]), ("success", "insufficient_results"))

    def test_explicit_empty_results(self):
        result = analyse_frames([frame(end=True), frame(end=True)], 3)
        self.assertEqual((result["status"], result["observed_count"]), ("success", 0))

    def test_stagnant_page_is_not_end(self):
        result = analyse_frames([frame([card(1)])]*3, 3, stop_reason="stalled")
        self.assertEqual(result["outcome"], "incomplete")
        self.assertFalse(result["end_verified"])

    def test_loading_invalidates_end(self):
        f = frame([card(1)], end=True)
        f["before"]["loading"] = f["after"]["loading"] = True
        self.assertFalse(analyse_frames([f, f], 3)["end_verified"])

    def test_starting_mid_feed_has_no_rank_proof(self):
        self.assertIn("feed_head_not_observed", analyse_frames([frame([card(1)], top=False)], 1)["issues"])

    def test_dom_changed_during_screenshot(self):
        f = frame([card(1)])
        f["after"]["results"] = [card(2)]
        self.assertFalse(analyse_frames([f], 1)["rank_verified"])

    def test_snapshot_does_not_prove_saved_image(self):
        f = frame([card(1)])
        del f["sha256"]
        self.assertIn("screenshot_evidence_missing", analyse_frames([f], 1)["issues"])

    def test_dimensions_must_match_snapshot_viewport(self):
        f = frame([card(1)])
        f["dimensions"] = [10, 10]
        self.assertIn("screenshot_viewport_dimension_mismatch", analyse_frames([f], 1)["issues"])

    def test_ambiguous_identity_not_insufficient_outcome(self):
        f = frame([card(1), card(2, href="")], end=True)
        self.assertEqual(analyse_frames([f, f], 3)["outcome"], "incomplete")

    def test_different_tab_or_multiple_feeds(self):
        for key, value in (("search_tab", "用户"), ("feed_count", 2)):
            f = frame([card(1)])
            f["before"][key] = f["after"][key] = value
            self.assertEqual(analyse_frames([f], 1)["status"], "partial")

    def test_block_on_later_frame_overrides_prior_success(self):
        f = frame([card(1)])
        f["after"]["login_modal"] = True
        self.assertEqual(adapter._evaluate([frame([card(1)]), f], {"keyword": "零跑", "capture_kind": "results", "top_n": 1})["status"], "blocked")

    def test_input_objects_are_immutable(self):
        f = frame([card(1)])
        old = deepcopy(f)
        analyse_frames([f], 1)
        self.assertEqual(f, old)


class ControlTests(unittest.TestCase):
    def test_1800_budget_and_absolute_cancel_file(self):
        req = adapter.validate_request(dict(keyword="零跑", capture_kind="results", output_dir=str(Path.cwd()),
            timeout_seconds=1800, cancel_file=str(Path.cwd()/"cancel")))
        self.assertEqual(req["timeout_seconds"], 1800)
        for bad in (1801, True, float("inf")):
            with self.assertRaises(ValueError):
                adapter.validate_request(dict(req, timeout_seconds=bad))
        with self.assertRaises(ValueError):
            adapter.validate_request(dict(req, cancel_file="relative"))

    def test_precancel_never_starts_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = Path(tmp)/"cancel"
            cancel.touch()
            with patch.object(adapter.subprocess, "Popen") as popen:
                result = adapter.capture(dict(keyword="零跑", capture_kind="results", output_dir=tmp, cancel_file=str(cancel)))
            popen.assert_not_called()
            self.assertEqual(result["status"], "cancelled")

    def test_expired_budget_and_cancel(self):
        with self.assertRaises(TimeoutError):
            adapter.Budget(1, deadline=0).check()
        with tempfile.TemporaryDirectory() as tmp:
            cancel = Path(tmp)/"cancel"
            budget = adapter.Budget(1, str(cancel))
            cancel.touch()
            with self.assertRaises(adapter.CaptureCancelled):
                budget.check()

    def test_profile_reuse_and_os_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)/"dedicated"
            with adapter._dedicated_profile(profile):
                self.assertEqual((profile/adapter.PROFILE_MARKER).read_text(encoding="utf-8"), adapter.PROFILE_OWNER)
                with self.assertRaises(OSError):
                    with adapter._dedicated_profile(profile):
                        pass
            with adapter._dedicated_profile(profile):
                pass

    def test_general_browser_profile_and_wrong_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile/"Preferences").write_text("fixture")
            with self.assertRaises(ValueError):
                with adapter._dedicated_profile(profile):
                    pass
            (profile/adapter.PROFILE_MARKER).write_text("wrong-owner")
            with self.assertRaises(ValueError):
                with adapter._dedicated_profile(profile):
                    pass

    def test_manual_login_only_dispatches_when_called(self):
        with patch.object(adapter, "_supervise", return_value={"status": "partial"}) as supervisor:
            adapter.open_login(str(Path.cwd()/"dedicated"), cancel_file=str(Path.cwd()/"cancel"))
        self.assertEqual(supervisor.call_args.kwargs["mode"], "login")
        self.assertEqual(supervisor.call_args.args[0]["timeout_seconds"], 1800)


if __name__ == "__main__":
    unittest.main()
