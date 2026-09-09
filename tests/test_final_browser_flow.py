"""最终新增行为的本地定点确认；不启动浏览器，不访问平台。"""
import unittest
from unittest.mock import MagicMock, patch

from opinion_capture.existing_browser import _Session
from opinion_capture.web_xiaohongshu import _next_scroll_step
from opinion_capture.web_xiaohongshu import _source_problem


class FinalBrowserFlowTests(unittest.TestCase):
    def test_home_recommendations_and_previous_query_are_not_keyword_suggestions(self):
        request = {"keyword":"零跑", "capture_kind":"suggestions"}
        state = {"url":"https://www.xiaohongshu.com/explore", "keyword":"零跑",
                 "input_id":"search-input-in-feeds", "input_visible":True,
                 "suggestions":[{"text":"日杂穿搭", "matched_queries":[]}]}
        self.assertEqual(_source_problem(state, request), "suggestion_query_not_verified")
        state.update(input_id="search-input-ai", url="https://www.xiaohongshu.com/search_result_ai?keyword=比亚迪")
        self.assertEqual(_source_problem(state, request), "suggestion_query_not_verified")
        state.update(input_id="search-input", url="https://www.xiaohongshu.com/search_result?keyword=零跑")
        self.assertIsNone(_source_problem(state, request))

    def test_new_batch_refreshes_once_same_batch_does_not(self):
        session, page, adapter = _Session(), MagicMock(), MagicMock()
        page.url = "https://search.bilibili.com/all?keyword=A1"
        adapter.capture_from_page.side_effect = lambda *_: {"status":"success"}
        request = {"platform":"bilibili","client":"WEB","keyword":"A1","batch_id":"one",
                   "browser_connection_mode":"dedicated","user_data_dir":"test-profile","timeout_seconds":5}
        with patch.object(session,"page",return_value=page), patch("opinion_capture.existing_browser.importlib.import_module",return_value=adapter):
            self.assertFalse(session.capture(request)["page_refreshed_for_new_batch"])
            self.assertFalse(session.capture(request)["page_refreshed_for_new_batch"])
            self.assertTrue(session.capture(dict(request,batch_id="two"))["page_refreshed_for_new_batch"])
        page.reload.assert_called_once()

    def test_final_scroll_only_covers_missing_target_bottom(self):
        state = {"viewport":{"height":1080},"scroll":{"viewport_height":964,"rect":[122,116,763,964]},
                 "results":[{"id":"n","rect":[130,1000,180,193]}]}
        analysis = {"items":[{"id":"n","counted":True,"screenshot_covered":False}]}
        self.assertEqual(_next_scroll_step(state,analysis,1),209)
        state["results"][0]["rect"][1] = 700
        self.assertEqual(_next_scroll_step(state,analysis,1),0)


if __name__ == "__main__":
    unittest.main()
