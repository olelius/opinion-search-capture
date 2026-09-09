"""合并修复的必要定点：无头切换、焦点、会话保存、失败状态及阶段事件。"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from opinion_capture import sessions, web_xiaohongshu as xhs
from opinion_capture.config import Plan
from opinion_capture.existing_browser import _Session
from opinion_capture.progress import phase
from opinion_capture.runner import run_plan
from opinion_capture.scheduling import TIMEZONE
from tests.test_web_xiaohongshu_page_reuse import PageDouble, page_state


class MergedRuntimeFixTests(unittest.TestCase):
    def test_course_collection_is_recorded_not_mistaken_for_missing_video_id(self):
        from opinion_capture.web_results_bilibili import normalize_snapshot, identity_order_failures
        scene={"items":[
            {"dom_index":1,"href":"https://www.bilibili.com/video/BV1234567890/","title":"独立视频"},
            {"dom_index":2,"href":"https://www.bilibili.com/cheese/play/ss773141969?query_from=0","title":"三角洲动画实战营"},
            {"dom_index":3,"href":"https://www.bilibili.com/video/","title":"身份尚未加载"}],"excluded":[]}
        normalize_snapshot(scene)
        self.assertEqual([i["dom_index"] for i in scene["items"]],[1,3])
        self.assertEqual(scene["excluded"][0]["reason"],"course_collection_not_independent_video")
        self.assertIn("card_id_missing:3",identity_order_failures(scene))
        self.assertNotIn("card_id_missing:2",identity_order_failures(scene))

    def test_switch_headless_closes_owned_profile_before_relaunch(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _Session()
            runtime = MagicMock()
            first, second = MagicMock(), MagicMock()
            for context in (first, second):
                context.browser.is_connected.return_value = True
                context.pages = [MagicMock()]
                context.pages[0].is_closed.return_value = False
            runtime.chromium.launch_persistent_context.side_effect = [first, second]
            request = {"platform":"xiaohongshu", "user_data_dir":directory, "browser_headless":True}
            with patch("playwright.sync_api.sync_playwright") as factory, \
                 patch.object(xhs,"_dedicated_profile") as manager, \
                 patch.object(xhs,"_browser_paths",return_value=[Path("chrome.exe")]):
                factory.return_value.start.return_value = runtime
                session._dedicated_page(request)
                session._dedicated_page(dict(request,browser_headless=False))
                self.assertEqual([c.kwargs["headless"] for c in runtime.chromium.launch_persistent_context.call_args_list],[True,False])
                first.close.assert_called_once()
                self.assertEqual(len(session.dedicated),1)
                session.disconnect()

    def test_suggestions_focus_does_not_click_or_search_again(self):
        class FocusPage(PageDouble):
            def focus(self, **kwargs):
                self.calls.append(("focus",))
                self.state.update(suggestion_panel_rect=[5,5,60,35], suggestion_panel_associated=True,
                                  suggestion_panel_complete=True, suggestions=[{"text":"零跑汽车","dom_index":0}])
        state = page_state()
        state["input_id"]="search-input"
        page = FocusPage(state)
        evidence={}
        with patch.object(xhs.Budget,"pause",lambda budget,*_:budget.check()):
            result=xhs._prepare_current_page(page,{"keyword":"零跑","capture_kind":"suggestions"},evidence,xhs.Budget(2))
        self.assertTrue(result["suggestion_panel_associated"])
        self.assertEqual(evidence["suggestion_focus_attempts"],1)
        self.assertFalse(any(c[0] in ("click","fill","goto") for c in page.calls))

    def test_verified_login_saved_but_closed_unknown_is_not_claimed_logged_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            plan=SimpleNamespace(output_root=str(root/"exports"),browser_profile_root=str(root/"profiles"))
            with patch.object(sessions.paths,"application_root",return_value=root), \
                 patch("opinion_capture.existing_browser.close_connection"), patch.object(sessions,"_run_login") as run:
                run.return_value={"status":"success","outcome":"authenticated_session_ready",
                                  "reason":"authenticated_navigation_visible","authentication_verified":True,"session_persisted":True}
                result=sessions.open_web_login(plan,"xiaohongshu",timeout_seconds=5)
                self.assertTrue(result["authentication_verified"])
                self.assertTrue(result["session_persisted"])
                run.return_value={"status":"success","outcome":"manual_session_closed","reason":"manual_window_closed_authentication_not_asserted"}
                result=sessions.open_web_login(plan,"bilibili",timeout_seconds=5)
                self.assertFalse(result["authentication_verified"])
                self.assertFalse(result["session_persisted"])

    def test_no_final_image_is_failed_and_phase_events_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            events=[]
            def collector(request):
                self.assertTrue(request["browser_headless"])
                phase("waiting_content","等待测试内容")
                time.sleep(.3)
                return {**{k:request[k] for k in ("platform","client","capture_kind","keyword")},
                        "status":"partial","reason":"click_action_timeout","rank_verified":False,
                        "observed_count":0,"requested_top_n":20,"primary_artifact_path":None,"artifact_paths":[],
                        "captured_at":datetime.now(TIMEZONE).isoformat()}
            plan=Plan(output_root=directory,keywords=["A1"],platforms=["xiaohongshu"],clients=["WEB"],
                      capture_kinds=["suggestions"],browser_headless=True,max_retries=0)
            result=run_plan(plan,collector=collector,progress_callback=events.append)
            self.assertEqual(result["results"][0]["status"],"failed")
            self.assertFalse(result["results"][0]["has_deliverable"])
            self.assertTrue(any(e.get("phase_code")=="waiting_content" for e in events))
            self.assertTrue(result["results"][0]["stage_timings"])


if __name__ == "__main__":
    unittest.main()
