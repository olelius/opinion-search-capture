"""本次浮层手势缺陷的纯函数检查，不连接ADB或重跑平台采集。"""

import time
import unittest
import xml.etree.ElementTree as ET

from opinion_capture.android import AdbClient, AndroidBlocked, _swipe_forward


class RecordingAdb:
    """仅复用手势编码，run截获命令；不创建真实ADB客户端。"""
    drag_hold = AdbClient.drag_hold

    def __init__(self):
        self.deadline = time.monotonic() + 5
        self.cancel_file = None
        self.commands = []

    def run(self, *command):
        self.commands.append(command)
        return b"gesture_completed\n"


class ScrollProgressTests(unittest.TestCase):
    def test_caption_below_cover_can_look_like_time(self):
        """位置证明时长/正文角色，不能因正文恰好是时间格式便将其丢弃。"""
        from opinion_capture.android_results import _note_text_fields
        body = ET.Element("node")
        cover = ET.SubElement(body, "node", {"class":"android.widget.ImageView", "bounds":"[0,0][500,500]"})
        ET.SubElement(body, "node", {"class":"android.widget.TextView", "text":"0:12", "bounds":"[400,460][480,480]"})
        ET.SubElement(body, "node", {"class":"android.widget.TextView", "text":"0:36", "bounds":"[0,510][500,550]"})
        author = ET.Element("node", {"bounds":"[0,570][500,610]"})
        self.assertEqual(_note_text_fields(body, cover, author), ("0:36", "0:12", False))

    def test_known_overlay_is_outside_gesture(self):
        """真实失败帧浮层从y1721起，导航动作全部位于其上方且避开两列间隙。"""
        adb = RecordingAdb()
        action = _swipe_forward(adb, {
            "platform": "xiaohongshu", "viewport": [0, 210, 1080, 1721],
            "occlusions": [{"bounds": [26, 1721, 1054, 1833]}],
        })
        self.assertEqual(action["start"][0], 810)
        self.assertTrue(210 < action["end"][1] < action["start"][1] < 1721)
        self.assertEqual(len(adb.commands), 1)
        self.assertEqual(adb.commands[0][0], "shell")
        self.assertIn(f"motionevent DOWN 810 {action['start'][1]}", adb.commands[0][1])
        self.assertIn(f"motionevent UP 810 {action['end'][1]}", adb.commands[0][1])
        self.assertEqual(action["end_hold_ms"], 180)

    def test_missing_visible_content_never_dispatches(self):
        adb = RecordingAdb()
        with self.assertRaises(AndroidBlocked):
            _swipe_forward(adb, {"platform": "xiaohongshu", "viewport": [0, 210, 1080, 300]})
        self.assertEqual(adb.commands, [])


if __name__ == "__main__":
    unittest.main()
