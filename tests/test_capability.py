"""V1.2 阶段 6 测试：行业规则数据能力状态与覆盖率分母。

全离线、确定性。覆盖：
- 银行资本充足率、保险偿付能力、券商净资本等无可靠数据字段的检查标记为
  unsupported_source，不计入「本次已执行的有效检查数量」；
- 不适用（NOT_APPLICABLE）优先于能力状态：银行规则对普通行业只是「不适用」；
- 覆盖率分母、维度桶、行业能力摘要正确；
- 报告区分「数据不足」与「数据源暂不支持」，不冒充已完成检查。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.models import (  # noqa: E402
    Capability,
    Market,
    RuleStatus,
    Security,
)
from app.engine.metrics import compute_metrics  # noqa: E402
from app.engine.normalize import FactSet  # noqa: E402
from app.engine.rules.base import EvidenceStore, RuleContext  # noqa: E402
from app.engine.runner import build_registry, run_rules  # noqa: E402
from app.engine.pipeline import ScanPipeline  # noqa: E402


def _context(pack: str = "general") -> RuleContext:
    sec = Security("000001", Market.A, "测试主体", "000001.SZ", industry="金融业")
    fs = FactSet([])
    return RuleContext(
        sec, Market.A, fs, compute_metrics(fs), [], {},
        EvidenceStore(), industry_pack=pack,
    )


class RuleCapabilityMarkingTests(unittest.TestCase):
    def test_unsupported_rule_ids(self):
        reg = build_registry()
        by_id = {r.rule_id: r for r in reg.rules}
        unsupported = {"BK03", "BK04", "IN03", "IN04", "BR03", "BR04"}
        for rid in unsupported:
            self.assertEqual(
                by_id[rid].capability, Capability.UNSUPPORTED_SOURCE.value,
                f"{rid} 应为 unsupported_source",
            )
        enabled = {"BK01", "BK02", "IN01", "IN02", "BR01", "BR02", "RE01", "FQ01"}
        for rid in enabled:
            self.assertEqual(
                by_id[rid].capability, Capability.ENABLED.value,
                f"{rid} 应为 enabled",
            )

    def test_exactly_six_unsupported_in_registry(self):
        reg = build_registry()
        unsupported = [r for r in reg.rules
                       if r.capability == Capability.UNSUPPORTED_SOURCE.value]
        self.assertEqual(len(unsupported), 6)
        # V1.3 新增八项通用检查，保留六项监管数据能力标注。
        self.assertEqual(len(reg.rules), 60)


class CoverageDenominatorTests(unittest.TestCase):
    def test_bank_unsupported_not_in_effective_checks(self):
        out = run_rules(_context("bank"), build_registry())
        cov = out.coverage
        self.assertEqual(cov.unsupported, 2, "银行包应有 2 项数据源暂不支持")
        # 覆盖率分母：适用 + 不适用 + 暂不支持 == 全部规则
        self.assertEqual(cov.applicable + cov.not_applicable + cov.unsupported, 60)
        # 已判断 + 数据不足 == 适用（暂不支持已排除在适用之外）
        self.assertEqual(cov.evaluated + cov.insufficient, cov.applicable)
        # BK03/BK04 不再进入数据不足计数
        by_id = {o.rule.rule_id: o for o in out.outcomes}
        self.assertEqual(by_id["BK03"].status, RuleStatus.INSUFFICIENT)
        self.assertNotIn("BK03", [o.rule.rule_id for o in out.outcomes
                                  if o.status is RuleStatus.INSUFFICIENT
                                  and o.capability == Capability.ENABLED.value])

    def test_general_pack_unsupported_is_zero(self):
        # 普通行业：银行/保险/券商规则为「不适用」，不是「数据源暂不支持」。
        out = run_rules(_context("general"), build_registry())
        self.assertEqual(out.coverage.unsupported, 0)
        self.assertEqual(out.coverage.applicable + out.coverage.not_applicable, 60)

    def test_insurance_broker_unsupported_count(self):
        for pack, expected in (("insurance", 2), ("broker", 2), ("realestate", 0)):
            with self.subTest(pack=pack):
                out = run_rules(_context(pack), build_registry())
                self.assertEqual(out.coverage.unsupported, expected, pack)

    def test_completeness_denominator_excludes_unsupported(self):
        out = run_rules(_context("bank"), build_registry())
        cov = out.coverage
        expected = round(cov.evaluated / cov.applicable, 4) if cov.applicable else 0.0
        self.assertEqual(cov.to_dict()["completeness"], expected)
        # 暂不支持项没有稀释覆盖率分母
        self.assertEqual(cov.applicable, cov.evaluated + cov.insufficient)

    def test_by_dimension_unsupported_bucket(self):
        out = run_rules(_context("bank"), build_registry())
        by_dim = out.coverage.by_dimension
        # BK03 属财务质量、BK04 属偿债能力
        self.assertEqual(by_dim["财务质量"]["数据源暂不支持"], 1)
        self.assertEqual(by_dim["偿债能力"]["数据源暂不支持"], 1)
        # 普通行业不产生「数据源暂不支持」桶计数
        out_g = run_rules(_context("general"), build_registry())
        for bucket in out_g.coverage.by_dimension.values():
            self.assertEqual(bucket.get("数据源暂不支持", 0), 0)


class CapabilitySummaryTests(unittest.TestCase):
    def test_bank_summary(self):
        out = run_rules(_context("bank"), build_registry())
        cap = ScanPipeline._capability_summary(out)
        self.assertEqual(cap["unsupported_source"], 2)
        ids = {r["rule_id"] for r in cap["unsupported_rules"]}
        self.assertEqual(ids, {"BK03", "BK04"})
        # enabled + unsupported == 适用于当前主体的检查数
        self.assertEqual(
            cap["enabled"] + cap["unsupported_source"],
            out.coverage.applicable + out.coverage.unsupported,
        )

    def test_general_summary_has_no_unsupported(self):
        out = run_rules(_context("general"), build_registry())
        cap = ScanPipeline._capability_summary(out)
        self.assertEqual(cap["unsupported_source"], 0)
        self.assertEqual(cap["unsupported_rules"], [])

    def test_collect_missing_excludes_unsupported(self):
        out = run_rules(_context("bank"), build_registry())
        missing = ScanPipeline._collect_missing(out)
        unsupported = ScanPipeline._collect_unsupported(out)
        missing_ids = {m["rule_id"] for m in missing}
        unsupported_ids = {u["rule_id"] for u in unsupported}
        self.assertEqual(unsupported_ids, {"BK03", "BK04"})
        self.assertTrue(unsupported_ids.isdisjoint(missing_ids),
                        "数据源暂不支持不应混入「数据不足」列表")

    def test_collect_unsupported_excludes_not_applicable(self):
        out = run_rules(_context("general"), build_registry())
        unsupported = ScanPipeline._collect_unsupported(out)
        self.assertEqual(unsupported, [],
                         "普通行业的银行/保险/券商规则是不适用，不列入暂不支持")


class ReportCapabilityRenderingTests(unittest.TestCase):
    def test_report_renders_unsupported_note(self):
        from tests.test_validation import _base_payload
        from app.report.render import render_inline

        p = _base_payload()
        p["capability_summary"] = {
            "enabled": 35, "unsupported_source": 2,
            "unsupported_rules": [{"rule_id": "BK03", "name": "资产质量与拨备",
                                   "dimension": "财务质量"}],
        }
        p["unsupported_data"] = [
            {"rule_id": "BK03", "name": "资产质量与拨备",
             "dimension": "财务质量", "reason": "不良率未包含在当前数据源"},
        ]
        html = render_inline(p)
        self.assertIn("数据源暂不支持", html)
        self.assertIn("未计入有效检查", html)
        self.assertIn("资产质量与拨备", html)

    def test_report_without_unsupported_still_renders(self):
        from tests.test_validation import _base_payload
        from app.report.render import render_inline

        # 旧报告无 capability_summary / unsupported_data 字段时仍能渲染
        html = render_inline(_base_payload())
        self.assertIn("排雷报告", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
