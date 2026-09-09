"""普通搜索入口的最小回归，不访问网页或重做整批测试。"""
import unittest
from unittest.mock import Mock
from opinion_capture import web_xiaohongshu as xhs
from tests.test_web_scroll_retry import FakeBudget, FakePage


class NormalEntryTests(unittest.TestCase):
    def request(self, page):
        return dict(keyword=page.keyword, capture_kind='results', top_n=20)

    def test_blank_ai_and_other_keyword_use_one_ordinary_navigation(self):
        for current in ('blank', 'ai', 'other'):
            with self.subTest(current=current):
                page = FakePage('xhs')
                request = self.request(page)
                if current == 'blank':
                    page.url = 'about:blank'
                elif current == 'ai':
                    page.url = page.url.replace('/search_result?', '/search_result_ai?')
                else:
                    request['keyword'] = '新关键词'
                evidence = {}
                ready = xhs._prepare_current_page(page, request, evidence, FakeBudget())
                self.assertTrue(xhs._normal_search_matches(ready, request['keyword']))
                self.assertEqual([c for c in page.calls if c[0] == 'goto'],
                                 [('goto', xhs._normal_search_url(request['keyword']))])
                self.assertFalse(any(c[0] in ('fill', 'press') for c in page.calls))
                self.assertNotIn('preparation_problem', evidence)

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
            page.url = page.url.replace('/search_result?', '/search_result_ai?')
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
