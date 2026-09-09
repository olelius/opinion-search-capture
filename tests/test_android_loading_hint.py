"""直播观看人数图标与列表加载页脚的最小结构回归，不访问设备。"""
import unittest

from opinion_capture.android import _loading_hint


ICON = '<node class="android.widget.ImageView" text="" bounds="[473,1824][501,1852]"/>'
SPINNER = '<node class="android.widget.ImageView" text="" bounds="[501,1843][579,1885]"/>'


def scene(body):
    return ('<hierarchy><node class="androidx.recyclerview.widget.RecyclerView" '
            'bounds="[0,42][1080,1920]">' + body + '</node></hierarchy>')


class LoadingHintTests(unittest.TestCase):
    def setUp(self):
        self.page = dict(platform="xiaohongshu", viewport=[0,210,1080,1920], items=[], excluded=[])

    def test_live_audience_icon_is_not_loading_even_without_semantic_classification(self):
        xml = scene('<node class="android.widget.FrameLayout" bounds="[8,1334][536,1877]">'
                    '<node class="android.widget.LinearLayout" clickable="true" bounds="[26,1817][518,1859]">'
                    '<node text="主播名字"/>' + ICON + '<node text="1"/></node></node>')
        self.assertIsNone(_loading_hint(xml, self.page))
        self.page['excluded'] = [dict(reason='live_room_not_note', bounds=[8,1334,536,1877])]
        self.assertIsNone(_loading_hint(xml, self.page))

    def test_only_separate_noninteractive_footer_can_supply_an_unlabelled_spinner(self):
        footer = '<node class="android.widget.FrameLayout" bounds="[0,1808][1080,1920]">' + SPINNER + '</node>'
        self.assertEqual(_loading_hint(scene(footer), self.page), 'bottom_spinner_candidate')
        self.assertIsNone(_loading_hint('<hierarchy>' + SPINNER + '</hierarchy>', self.page))
        self.assertIsNone(_loading_hint(scene(footer.replace('<node class="android.widget.FrameLayout"',
                                                              '<node clickable="true" class="android.widget.FrameLayout"')), self.page))
        self.page['excluded'] = [dict(reason='account_aggregate', bounds=[0,1800,1080,1920])]
        self.assertIsNone(_loading_hint(scene(footer), self.page))

    def test_explicit_loading_text_and_progress_bar_still_work(self):
        self.assertEqual(_loading_hint(scene('<node text="正在加载数据"/>'), self.page), 'loading_text')
        self.assertEqual(_loading_hint(scene('<node class="android.widget.ProgressBar"/>'), self.page), 'progress_bar')


if __name__ == '__main__':
    unittest.main()
