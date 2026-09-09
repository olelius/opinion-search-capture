"""普通搜索入口的最小回归，不访问网页或重做整批测试。"""
import unittest
from unittest.mock import Mock
from opinion_capture import web_xiaohongshu as xhs
from tests.test_web_scroll_retry import FakeBudget, FakePage
from types import SimpleNamespace


class OrdinaryPage(FakePage):
    """模拟已证实的普通搜索按钮，不把页内提交伪装为显式goto。"""
    def locator(self, selector):
        if selector.startswith('.submit-button-wrapper'):
            return SimpleNamespace(count=lambda:1, click=lambda **kwargs:self.submit())
        return self

    def submit(self):
        self.calls.append(('search_button',))
        self.url = xhs._normal_search_url(self.keyword)
        self.generation += 1
        self.top, self.panel = 0, False


class NormalEntryTests(unittest.TestCase):
    def request(self, page):
        return dict(keyword=page.keyword, capture_kind='results', top_n=20)

    def test_cold_bootstrap_once_and_ai_switch_once(self):
        for current in ('blank', 'ai'):
            with self.subTest(current=current):
                page = FakePage('xhs')
                request = self.request(page)
                if current == 'blank':
                    page.url = 'about:blank'
                elif current == 'ai':
                    page.url = page.url.replace('/search_result?', '/search_result_ai?')
                evidence = {}
                ready = xhs._prepare_current_page(page, request, evidence, FakeBudget())
                self.assertTrue(xhs._normal_search_matches(ready, request['keyword']))
                expected = [('goto', xhs._normal_search_url(request['keyword']))]
                if current == 'blank':
                    expected.insert(0, ('goto','https://www.xiaohongshu.com/explore'))
                self.assertEqual([c for c in page.calls if c[0] == 'goto'], expected)
                self.assertFalse(any(c[0] in ('fill', 'press') for c in page.calls))
                self.assertNotIn('preparation_problem', evidence)

    def test_ordinary_keyword_change_uses_fill_and_native_button_not_goto(self):
        page = OrdinaryPage('xhs')
        request = dict(self.request(page), keyword='新关键词')
        evidence = {}
        ready = xhs._prepare_current_page(page, request, evidence, FakeBudget())
        self.assertEqual(ready['keyword'], '新关键词')
        self.assertFalse(any(c[0] == 'goto' for c in page.calls))
        self.assertEqual([c for c in page.calls if c[0] == 'fill'], [('fill','新关键词')])
        self.assertEqual(sum(c[0] == 'search_button' for c in page.calls), 1)
        self.assertEqual(evidence['search_control'], 'platform_search_button')
        self.assertEqual(evidence['search_submit_attempts'], 1)

    def test_navigation_timeout_records_ready_page_without_resending(self):
        page = OrdinaryPage('xhs')
        page.goto = Mock(side_effect=TimeoutError('load event did not arrive'))
        evidence = {}
        state = xhs._open_search_page(page, page.url, evidence, FakeBudget())
        self.assertTrue(xhs._ordinary_surface(state))
        self.assertTrue(evidence['navigation_records'][0]['continued_from_visible_page'])
        page.goto.assert_called_once()

    def test_hidden_sort_uses_latest_matching_native_response_not_all_label(self):
        page = OrdinaryPage('xhs')
        page.url = xhs._normal_search_url(page.keyword)
        handlers = {}
        page.on = lambda event, handler: handlers.update({event:handler})
        xhs._observe_native_search(page)
        original = page.evaluate
        def evaluate(*args):
            state = original(*args)
            state['search_tab'] = ''
            return state
        page.evaluate = evaluate
        req = SimpleNamespace(url='https://edith.xiaohongshu.com/api/sns/web/v1/search/notes',
                              post_data_json=dict(keyword=page.keyword, sort='general', note_type=0))
        handlers['request'](req)
        self.assertEqual(xhs._snapshot(page, FakeBudget())['search_tab'], '')
        handlers['response'](SimpleNamespace(request=req, status=200))
        self.assertEqual(xhs._snapshot(page, FakeBudget())['search_tab'], '综合')
        latest = SimpleNamespace(url=req.url, post_data_json=dict(keyword=page.keyword, sort='time_descending',note_type=0))
        handlers['request'](latest)
        handlers['response'](SimpleNamespace(request=req,status=200))
        self.assertEqual(xhs._snapshot(page, FakeBudget())['search_tab'], '')
        handlers['response'](SimpleNamespace(request=latest,status=200))
        self.assertNotEqual(xhs._snapshot(page, FakeBudget())['search_tab'], '综合')

    def test_uncovered_bottom_card_is_centered_before_its_top_can_be_lost(self):
        state = dict(viewport=dict(height=1080), scroll=dict(viewport_height=1080, rect=[0,0,1440,1080]),
                     results=[dict(id='target',rect=[200,966,200,392],coverage=None),
                              dict(id='later',rect=[400,1800,200,300],coverage=None)])
        analysis = dict(items=[dict(id=key,counted=True,screenshot_covered=False) for key in ('target','later')])
        step = xhs._next_scroll_step(state, analysis, 2)
        self.assertEqual(step, 622)
        self.assertEqual(state['results'][0]['rect'][1] - step, 344)
        self.assertLess(step, 1080 * .8)

    def test_normal_same_keyword_reuses_and_retry_reloads_once(self):
        page = FakePage('xhs')
        request = self.request(page)
        xhs._prepare_current_page(page, request, {}, FakeBudget())
        self.assertFalse(any(c[0] == 'goto' for c in page.calls))
        request['fresh_search'] = True
        xhs._prepare_current_page(page, request, {}, FakeBudget())
        self.assertEqual(sum(c[0] == 'goto' for c in page.calls), 1)

    def test_redirect_back_to_ai_does_not_pass_as_ordinary_surface(self):
        page = FakePage('xhs')
        request = self.request(page)
        page.url = 'https://www.xiaohongshu.com/explore'
        original = page.goto
        def redirect(url, **kwargs):
            original(url, **kwargs)
            page.url = page.url.replace('/search_result', '/search_result_ai', 1)
        page.goto = redirect
        evidence = {}
        xhs._prepare_current_page(page, request, evidence, FakeBudget())
        self.assertEqual(evidence['preparation_problem'], 'ordinary_search_surface_not_confirmed')
        self.assertEqual(sum(c[0] == 'goto' for c in page.calls), 1)

    def test_document_replacement_only_retries_read_and_other_errors_propagate(self):
        page = Mock()
        page.evaluate.side_effect = [RuntimeError('Execution context was destroyed'), {'keyword':'C10'}]
        budget = FakeBudget()
        self.assertEqual(xhs._snapshot(page, budget)['keyword'], 'C10')
        self.assertEqual(budget.pauses, [100])
        page.goto.assert_not_called()
        page.evaluate.side_effect = RuntimeError('different failure')
        with self.assertRaisesRegex(RuntimeError, 'different failure'):
            xhs._snapshot(page, budget)


if __name__ == '__main__':
    unittest.main()
