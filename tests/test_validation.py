"""V1.2 阶段 0 测试：固定验收集与报告语义差异工具。

全离线、确定性。覆盖：
- 验收集结构合法（≥20 家、A/H 覆盖、五行业包覆盖、pending_review 不编造标签、≥3 基准样本）；
- 语义差异工具：只改时间字段判语义一致；改财务值/规则状态/证据引文/缺口必须检出；
- 模型差异与元数据差异的分类。
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.validation.checklist import (  # noqa: E402
    baseline_entries,
    load_checklist,
    validate_checklist,
)
from app.validation.semantic_diff import (  # noqa: E402
    compare_reports,
    semantic_fingerprint,
)


def _base_payload() -> dict:
    return {
        "report_version": "1.1",
        "rule_version": "1.1",
        "task_id": "taskA",
        "generated_at": "2026-09-06T12:00:00",
        "scan": {"started_at": "2026-09-06T11:59:00", "elapsed_seconds": 61.0, "timed_out": False, "status": "完成"},
        "security": {"code": "600519", "market": "A", "name": "贵州茅台", "secucode": "600519.SH", "exchange": "上交所"},
        "data_scope": {"announcement_range": "2025-09-06 ~ 2026-09-06", "evidence_verified": 50},
        "summary": {"risk_count": 1, "watch_count": 0, "insufficient_count": 6},
        "metrics": {"currency": "CNY", "latest_period": "2026-06-30", "items": {"revenue": {"value": 100.0}}},
        "dimensions": [
            {"dimension": "财务质量", "results": [
                {"rule_id": "FQ08", "name": "报告期净利润为负", "status": "发现风险", "severity": "高", "finding": "净利润为负"},
            ]},
        ],
        "timeline": [{"event_id": "evt:doc1", "title": "处罚公告", "occurred_date": "2026-01-01"}],
        "gaps": ["缺少上期数据"],
        "evidence": {"doc1:p1": {"evidence_id": "doc1:p1", "quote": "本公司净利润为负", "location": "第 1 页", "verified": True}},
        "documents": [{"doc_id": "doc1", "title": "年报", "local_path": "/tmp/a.pdf", "sha256": "abc", "fetched_at": "2026-09-06T11:59:00", "page_count": 100}],
        "financial_facts": [{"std_item": "net_profit", "value": -100.0, "period_end": "2026-06-30", "fetched_at": "2026-09-06T11:59:00"}],
        "ai": {"usage": {"calls": 2, "spent_cny": 0.02}, "notes": ["AI 解读完成"], "verification": []},
    }


class ChecklistTests(unittest.TestCase):
    def test_checklist_is_valid(self):
        self.assertEqual(validate_checklist(), [])

    def test_at_least_20_companies(self):
        data = load_checklist()
        self.assertGreaterEqual(len(data["entries"]), 20)

    def test_covers_both_markets(self):
        markets = {e["market"] for e in load_checklist()["entries"]}
        self.assertIn("A", markets)
        self.assertIn("HK", markets)

    def test_covers_all_industry_packs(self):
        packs = {e["industry_pack"] for e in load_checklist()["entries"]}
        self.assertTrue({"general", "bank", "insurance", "broker", "realestate"} <= packs)

    def test_baseline_samples_present(self):
        entries = baseline_entries()
        secucodes = {e["secucode"] for e in entries}
        self.assertGreaterEqual(len(entries), 3)
        self.assertTrue({"600519.SH", "00700.HK", "000002.SZ"} <= secucodes)

    def test_pending_review_has_no_fabricated_labels(self):
        for e in load_checklist()["entries"]:
            if e["review_status"] == "pending_review":
                self.assertEqual(e["expected_triggers"], [])
                self.assertEqual(e["forbidden_triggers"], [])
                self.assertEqual(e["evidence_location"], "")
                self.assertEqual(e["annotator"], "")


class SemanticDiffTests(unittest.TestCase):
    def test_time_only_change_is_semantically_identical(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["task_id"] = "taskB"
        b["generated_at"] = "2026-09-07T09:00:00"
        b["scan"]["started_at"] = "2026-09-07T08:59:00"
        b["scan"]["elapsed_seconds"] = 99.9
        b["documents"][0]["fetched_at"] = "2026-09-07T08:59:00"
        b["documents"][0]["sha256"] = "different"
        b["financial_facts"][0]["fetched_at"] = "2026-09-07T08:59:00"
        result = compare_reports(a, b)
        self.assertTrue(result["semantically_identical"], result)
        self.assertTrue(result["deterministic_identical"])
        self.assertEqual(result["deterministic_diffs"], [])

    def test_financial_value_change_detected(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["financial_facts"][0]["value"] = -999.0
        result = compare_reports(a, b)
        self.assertFalse(result["semantically_identical"])
        self.assertTrue(any("financial_facts" in d["path"] for d in result["deterministic_diffs"]))

    def test_rule_status_change_detected(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["dimensions"][0]["results"][0]["status"] = "已覆盖资料中未发现明显异常"
        result = compare_reports(a, b)
        self.assertFalse(result["deterministic_identical"])
        self.assertTrue(any("status" in d["path"] for d in result["deterministic_diffs"]))

    def test_evidence_quote_change_detected(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["evidence"]["doc1:p1"]["quote"] = "完全不同的原文片段"
        result = compare_reports(a, b)
        self.assertFalse(result["deterministic_identical"])
        self.assertTrue(any("evidence" in d["path"] for d in result["deterministic_diffs"]))

    def test_gap_change_detected(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["gaps"].append("新增：公告清单获取失败")
        result = compare_reports(a, b)
        self.assertFalse(result["deterministic_identical"])
        self.assertTrue(any("gaps" in d["path"] for d in result["deterministic_diffs"]))

    def test_model_diff_categorized_separately(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["ai"]["notes"] = ["AI 解读失败：模型截断"]
        result = compare_reports(a, b)
        self.assertFalse(result["model_identical"])
        self.assertTrue(result["deterministic_identical"])
        self.assertTrue(any("ai" in d["path"] for d in result["model_diffs"]))

    def test_metadata_diff_does_not_break_identity(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["documents"][0]["local_path"] = "/tmp/b.pdf"
        result = compare_reports(a, b)
        self.assertTrue(result["semantically_identical"])
        self.assertTrue(any("local_path" in d["path"] for d in result["metadata_diffs"]))

    def test_fingerprint_stable_for_time_change(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["task_id"] = "other"
        b["generated_at"] = "2026-10-01T00:00:00"
        self.assertEqual(semantic_fingerprint(a), semantic_fingerprint(b))

    def test_fingerprint_differs_for_value_change(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        b["financial_facts"][0]["value"] = 1.0
        self.assertNotEqual(semantic_fingerprint(a), semantic_fingerprint(b))

    def test_missing_key_detected(self):
        a = _base_payload()
        b = copy.deepcopy(a)
        del b["security"]["name"]
        result = compare_reports(a, b)
        self.assertFalse(result["deterministic_identical"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
