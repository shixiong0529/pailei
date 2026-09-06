"""V1.2 阶段 2 测试：AI 候选事件与正式事件分层。

全离线、确定性。覆盖：
- 合法 doc_id + 完整引文 + 正确页码 + 指纹 → 升级正式事件并绑定 verified evidence；
- 虚构引文 / 错误 doc_id / 同前缀但尾部不同 / 结构非法 / 非法类型 → 拒收；
- 无 verified evidence 的候选不能进入正式时间线，进入待核实线索；
- 报告区分已确认事件与待核实线索，无待核实线索时不渲染空区块。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core import db  # noqa: E402
from app.core.models import DisclosureDoc  # noqa: E402
from app.data.pdftext import ParsedDoc  # noqa: E402
from app.engine.pipeline import ScanPipeline  # noqa: E402
from app.engine.rules.base import EvidenceStore  # noqa: E402
from app.llm.adapter import LLMResult  # noqa: E402


def _doc(did="doc1", title="公告标题", dtype="其他公告"):
    return DisclosureDoc(did, "000001.SZ", title, "2026-01-05", "cninfo",
                         "https://example.org/a.pdf", doc_type=dtype)


class EventLayeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pailei-events-")
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(settings, "db_path", self.root / "app.db"),
            patch.object(settings, "reports_dir", self.root / "reports"),
            patch.object(settings, "files_dir", self.root / "files"),
        ]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _pipe_with_model(self, items):
        pipe = ScanPipeline("event_test")
        pipe.llm = SimpleNamespace(
            available=True,
            extract_events=lambda x: LLMResult(True, data=items),
        )
        return pipe

    def test_valid_candidate_promoted_with_verified_evidence(self):
        quote = "公司因信息披露违规受到行政处罚"
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, "正文开头。" + quote + "。正文结尾。")], False)
        store = EvidenceStore()
        pipe = self._pipe_with_model([
            {"doc_id": "doc1", "title": "行政处罚", "category": "监管处罚",
             "summary": "公司被处罚", "evidence_quote": quote, "resolved": False,
             "resolution_note": ""},
        ])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, store)
        finally:
            pipe.close()
        self.assertEqual(len(formal), 1)
        self.assertEqual(formal[0].category, "监管处罚")
        self.assertTrue(formal[0].evidence_ids, "正式事件必须绑定至少一条证据")
        self.assertEqual(len(clues), 0)
        # 证据已加入 store 并通过复核
        self.assertEqual(len(store.items), 1)
        ev = next(iter(store.items.values()))
        self.assertTrue(ev.verified)

    def test_fabricated_quote_rejected(self):
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, "这是真实的正文内容。")], False)
        store = EvidenceStore()
        pipe = self._pipe_with_model([
            {"doc_id": "doc1", "title": "处罚", "category": "监管处罚",
             "summary": "x", "evidence_quote": "完全虚构不存在的引文"},
        ])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, store)
        finally:
            pipe.close()
        self.assertEqual(formal, [])
        self.assertEqual(len(clues), 1)
        self.assertIn("未定位到完整引文", clues[0]["reason"])

    def test_same_prefix_but_different_tail_rejected(self):
        real = "公司确认债务违约"
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, real + "十亿元。")], False)
        store = EvidenceStore()
        pipe = self._pipe_with_model([
            {"doc_id": "doc1", "title": "违约", "category": "诉讼",
             "summary": "x", "evidence_quote": real + "一百亿元"},
        ])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, store)
        finally:
            pipe.close()
        self.assertEqual(formal, [])
        self.assertEqual(len(clues), 1)

    def test_illegal_category_rejected(self):
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, "真实引文内容")], False)
        pipe = self._pipe_with_model([
            {"doc_id": "doc1", "title": "x", "category": "不存在的事件类型",
             "summary": "x", "evidence_quote": "真实引文内容"},
        ])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, EvidenceStore())
        finally:
            pipe.close()
        self.assertEqual(formal, [])
        self.assertEqual(len(clues), 1)
        self.assertIn("固定枚举", clues[0]["reason"])

    def test_missing_evidence_quote_rejected(self):
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, "正文")], False)
        pipe = self._pipe_with_model([
            {"doc_id": "doc1", "title": "x", "category": "监管处罚", "summary": "x"},
        ])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, EvidenceStore())
        finally:
            pipe.close()
        self.assertEqual(formal, [])
        self.assertEqual(len(clues), 1)
        self.assertIn("evidence_quote 为空", clues[0]["reason"])

    def test_wrong_doc_id_rejected(self):
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, "正文")], False)
        pipe = self._pipe_with_model([
            {"doc_id": "doc2", "title": "x", "category": "监管处罚",
             "summary": "x", "evidence_quote": "正文"},
        ])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, EvidenceStore())
        finally:
            pipe.close()
        self.assertEqual(formal, [])
        self.assertEqual(len(clues), 1)
        self.assertIn("不在本批输入", clues[0]["reason"])

    def test_non_dict_item_ignored(self):
        d = _doc()
        p = ParsedDoc("doc1", 1, [(1, "正文")], False)
        pipe = self._pipe_with_model([42, "not-a-dict"])
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, EvidenceStore())
        finally:
            pipe.close()
        self.assertEqual(formal, [])
        self.assertEqual(clues, [])

    def test_deterministic_event_still_formal(self):
        # 即使模型不可用，doc_type 命中仍产生确定性正式事件。
        d = _doc(dtype="监管处罚")
        p = ParsedDoc("doc1", 1, [(1, "正文")], False)
        pipe = ScanPipeline("event_test")
        pipe.llm = SimpleNamespace(available=False)
        try:
            formal, clues = pipe._extract_events([d], {"doc1": p}, EvidenceStore())
        finally:
            pipe.close()
        self.assertEqual(len(formal), 1)
        self.assertEqual(formal[0].source_doc_id, "doc1")
        self.assertEqual(clues, [])


class EventReportTests(unittest.TestCase):
    def test_pending_clues_section_only_when_present(self):
        from app.report.render import render_inline
        from tests.test_validation import _base_payload

        # 无待核实线索时不显示空区块
        p = _base_payload()
        p["pending_clues"] = []
        p["timeline"] = []
        html = render_inline(p)
        self.assertNotIn("待核实线索", html)

        # 有待核实线索时显示
        p["pending_clues"] = [{"doc_id": "d", "title": "线索", "category": "监管处罚",
                               "summary": "s", "evidence_quote": "q", "occurred_date": "2026-01-01",
                               "reason": "未通过"}]
        html = render_inline(p)
        self.assertIn("待核实线索", html)

    def test_confirmed_timeline_section(self):
        from app.report.render import render_inline
        from tests.test_validation import _base_payload

        p = _base_payload()
        p["timeline"] = [{"event_id": "evt:doc1", "title": "处罚", "occurred_date": "2026-01-01",
                          "category": "监管处罚", "summary": "s", "resolved": False,
                          "evidence_ids": ["doc1:p1"]}]
        p["pending_clues"] = []
        html = render_inline(p)
        self.assertIn("已确认事件时间线", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
