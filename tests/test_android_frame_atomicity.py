"""针对新失败的贴边锚点与半帧提交回归；只读取已有证据，不访问设备。"""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from opinion_capture.android_results import EvidenceError, RankLedger, match_frames, parse_page

ROOT = Path(__file__).resolve().parents[1]
FAILED = ROOT / ".local/completion/app-final-20260908-231446-945f8e/bilibili/20260908-231609-339cf6fffb/result.json"


class AtomicFrameTests(unittest.TestCase):
    def item(self, identity, top):
        return dict(identity=identity, title=identity, author="作者", duration="", complete=True,
                    bounds=[10, top, 190, top+40], cover_bounds=[10, top+1, 100, top+20])

    def frame(self, items, index=0):
        return dict(png_path="not-an-image.png", xml_path="fixture.xml", source_index=index,
                    page=dict(items=items, viewport=[0, 0, 200, 500], recognized=True,
                              keyword_verified=True, scope_verified=True, excluded=[], unknown=[]))

    def test_late_card_failure_keeps_every_prior_collection_unchanged(self):
        """一帧先加入新卡、再碰到旧卡身份冲突，计数/链接/映射均回滚。"""
        ledger = RankLedger(20)
        first = self.frame([self.item("existing",20)])
        with patch.object(RankLedger,"_cover_hash",return_value="cover-one"):
            ledger.add(first)
        before = deepcopy((ledger.items, ledger.frames, ledger.links, ledger.unknown, ledger.excluded))
        rejected = self.frame([self.item("new",60),self.item("existing",120)],1)
        with patch("opinion_capture.android_results.match_frames",return_value={"shift":100}), \
             patch.object(RankLedger,"_cover_hash",return_value="different-content"):
            with self.assertRaisesRegex(EvidenceError,"identity_collision"):
                ledger.add(rejected)
        self.assertEqual((ledger.items,ledger.frames,ledger.links,ledger.unknown,ledger.excluded),before)
        self.assertNotIn("offset",rejected)
        self.assertNotIn("accepted",rejected)

    @unittest.skipUnless(FAILED.is_file(),"失败原始证据仅保存在本机")
    def test_saved_clipped_top_is_not_disagreeing_anchor(self):
        """只重算先前失败的frame001→002；未裁切共同卡和RGB均证明396px。"""
        report=json.loads(FAILED.read_text(encoding="utf-8"))
        previous,current=deepcopy(report["frames"][-2:])
        fresh=parse_page(Path(current["xml_path"]).read_text(encoding="utf-8"),"bilibili","results","比亚迪")
        clipped=next(i for i in fresh["items"] if i["identity"].startswith("cc705ab9"))
        self.assertEqual(clipped["bounds"][1],fresh["viewport"][1])
        self.assertFalse(clipped["complete"])
        link=match_frames(previous,current)
        self.assertEqual(link["shift"],396)
        self.assertLessEqual(link["mean_error"],1.5)
        self.assertNotIn(clipped["identity"],link["anchor_ids"])


if __name__ == "__main__":
    unittest.main()
