"""V1.2 阶段 3 测试：事件生命周期与按需历史追溯。

全离线、确定性。覆盖：
- 同一事项多份进展公告合并为一个生命周期，不重复计数；
- 解除依据必须来自后续正式披露，未解除状态保留，时间经过不构成解除；
- 关联键优先级：案件号 > 公告编号 > 标题关键词；
- 历史追溯只在存在未解除重要事件时启动，数量与时间上限有效。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.models import DisclosureDoc, RiskEvent  # noqa: E402
from app.engine import lifecycle  # noqa: E402


def _evt(eid, title, d, category, resolved=None):
    return RiskEvent(eid, title, d, category, "摘要", source_doc_id="d", resolved=resolved)


def _doc(did, title, d, dtype="其他公告"):
    return DisclosureDoc(did, "000001.SZ", title, d, "cninfo",
                         "https://example.org/a.pdf", doc_type=dtype)


CASE_TITLES = [
    "关于收到行政处罚的公告（2026）京0105民初123号",
    "关于行政处罚的进展公告（2026）京0105民初123号",
    "关于行政处罚结案的公告（2026）京0105民初123号",
]


class DedupKeyTests(unittest.TestCase):
    def test_case_number_takes_priority(self):
        title = "关于诉讼进展的公告（2026）粤0304民初88号"
        self.assertTrue(lifecycle.dedup_key(title).startswith("case:"))

    def test_announcement_number_fallback(self):
        title = "关于公司治理的公告 公告编号：2026-045"
        self.assertEqual(lifecycle.dedup_key(title), "ann:2026-045")

    def test_title_keyword_fallback(self):
        title = "关于对外担保的公告"
        key = lifecycle.dedup_key(title)
        self.assertTrue(key.startswith("title:"))
        # 相同核心关键词、不同包装词应归为同一键
        self.assertEqual(key, lifecycle.dedup_key("关于对外担保的补充公告"))

    def test_same_case_number_same_key(self):
        keys = {lifecycle.dedup_key(t) for t in CASE_TITLES}
        self.assertEqual(len(keys), 1)


class LifecycleTests(unittest.TestCase):
    def test_merge_progress_announcements_into_one_lifecycle(self):
        events = [
            _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚"),
            _evt("e2", CASE_TITLES[1], "2026-03-01", "监管处罚"),
            _evt("e3", CASE_TITLES[2], "2026-06-01", "监管处罚"),
        ]
        docs = [_doc(f"d{i}", t, events[i].occurred_date) for i, t in enumerate(CASE_TITLES)]
        enriched = lifecycle.enrich_events(events, docs)
        groups = lifecycle.build_lifecycles(enriched)
        self.assertEqual(len(groups), 1, "同一事项多份公告必须合并为一个生命周期")
        self.assertEqual(groups[0]["announcement_count"], 3)
        self.assertEqual(groups[0]["first_occurred"], "2026-01-01")
        self.assertEqual(groups[0]["latest_date"], "2026-06-01")

    def test_resolution_basis_from_formal_disclosure(self):
        events = [
            _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚"),
        ]
        docs = [
            _doc("d1", CASE_TITLES[0], "2026-01-01"),
            _doc("d3", CASE_TITLES[2], "2026-06-01"),  # 结案
        ]
        enriched = lifecycle.enrich_events(events, docs)
        self.assertTrue(enriched[0].resolved)
        self.assertIn("结案", enriched[0].resolution_basis)
        self.assertEqual(enriched[0].resolution_date, "2026-06-01")
        self.assertEqual(enriched[0].lifecycle_stage, "已解除")

    def test_unresolved_when_no_formal_resolution(self):
        events = [_evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚")]
        docs = [_doc("d1", CASE_TITLES[0], "2026-01-01")]
        enriched = lifecycle.enrich_events(events, docs)
        self.assertIsNot(enriched[0].resolved, True)
        groups = lifecycle.build_lifecycles(enriched)
        self.assertFalse(groups[0]["resolved"])

    def test_time_passing_alone_does_not_resolve(self):
        # 仅有更晚的进展公告、但标题无解除关键词，不能判为已解除。
        events = [_evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚")]
        docs = [
            _doc("d1", CASE_TITLES[0], "2026-01-01"),
            _doc("d2", CASE_TITLES[1], "2026-03-01"),  # 进展，非解除
        ]
        enriched = lifecycle.enrich_events(events, docs)
        self.assertIsNot(enriched[0].resolved, True)

    def test_no_double_counting(self):
        events = [
            _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚"),
            _evt("e2", CASE_TITLES[1], "2026-03-01", "监管处罚"),
        ]
        docs = [_doc(f"d{i}", t, events[i].occurred_date) for i, t in enumerate(CASE_TITLES[:2])]
        groups = lifecycle.build_lifecycles(lifecycle.enrich_events(events, docs))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["announcement_count"], 2)

    def test_enrich_sets_stage_and_order(self):
        events = [
            _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚"),
            _evt("e2", CASE_TITLES[1], "2026-03-01", "监管处罚"),
        ]
        docs = [_doc(f"d{i}", t, events[i].occurred_date) for i, t in enumerate(CASE_TITLES[:2])]
        enriched = lifecycle.enrich_events(events, docs)
        self.assertEqual({e.lifecycle_stage for e in enriched}, {"首次发生", "最新进展"})
        self.assertEqual({e.occurrence_order for e in enriched}, {1, 2})


class TraceBackTests(unittest.TestCase):
    def test_not_triggered_without_unresolved_important_event(self):
        resolved = _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚", resolved=True)
        self.assertFalse(lifecycle.should_trace_back([resolved]))
        trivial = _evt("e2", "关于高管变动的公告", "2026-01-01", "高管变动")
        self.assertFalse(lifecycle.should_trace_back([trivial]))

    def test_triggered_for_unresolved_important_event(self):
        ev = _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚")
        self.assertTrue(lifecycle.should_trace_back([ev]))

    def test_plan_limits_and_range(self):
        ev = _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚")
        limits = lifecycle.TraceBackLimits(max_queries=5, max_announcements=40,
                                           max_downloads=7, max_seconds=90.0)
        plan = lifecycle.trace_back_plan([ev], end=date(2026, 9, 6), max_years=3, limits=limits)
        self.assertEqual(plan.categories, ["监管处罚"])
        self.assertEqual(plan.start[:4], "2023")
        self.assertEqual(plan.end, "2026-09-06")
        self.assertEqual(plan.limits.max_queries, 5)
        self.assertEqual(plan.limits.max_downloads, 7)
        self.assertFalse(plan.empty)

    def test_plan_max_years_respected(self):
        ev = _evt("e1", CASE_TITLES[0], "2026-01-01", "监管处罚")
        plan = lifecycle.trace_back_plan([ev], end=date(2026, 9, 6), max_years=5)
        self.assertEqual(plan.start[:4], "2021")

    def test_plan_empty_when_nothing_to_trace(self):
        plan = lifecycle.trace_back_plan([], end=date(2026, 9, 6))
        self.assertTrue(plan.empty)
        self.assertEqual(plan.categories, [])


class LifecycleReportTests(unittest.TestCase):
    def test_lifecycle_section_renders_only_when_present(self):
        from app.report.render import render_inline
        from tests.test_validation import _base_payload

        p = _base_payload()
        p["lifecycles"] = []
        p["timeline"] = []
        p["pending_clues"] = []
        html = render_inline(p)
        self.assertNotIn("事件生命周期", html)

        p["lifecycles"] = [{
            "key": "case:x", "category": "监管处罚", "title": "处罚",
            "first_occurred": "2026-01-01", "latest_date": "2026-06-01",
            "announcement_count": 2, "resolved": True,
            "resolution_basis": "结案公告", "resolution_date": "2026-06-01",
            "related_doc_ids": ["d1"], "event_ids": ["e1"],
        }]
        html = render_inline(p)
        self.assertIn("事件生命周期", html)
        self.assertIn("已解除", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
