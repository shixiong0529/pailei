"""确定性与边界测试。

覆盖方案 §11 要求的边界样本：负利润、零分母、币种与单位、
累计值与单季值、证据真实性、审计意见误报排除。
不依赖网络，全部使用构造样本，可离线运行。

运行：
    python tests/run_tests.py
    或：python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.models import (  # noqa: E402
    Evidence,
    FinancialFact,
    Market,
    PeriodType,
    Statement,
)
from app.core.text import evidence_keywords, to_traditional  # noqa: E402
from app.data.pdftext import ParsedDoc, scan_audit_opinions, verify_evidence  # noqa: E402
from app.engine.normalize import FactSet, growth, safe_div  # noqa: E402
from app.engine.rules.industry import classify_industry  # noqa: E402
from app.report.render import risk_signal_score, safe_url  # noqa: E402


def fact(std_item: str, period: str, value: float, ptype=PeriodType.ANNUAL, **kw):
    return FinancialFact(
        secucode="TEST.SZ",
        statement=kw.pop("statement", Statement.INCOME),
        raw_item=kw.pop("raw_item", std_item.upper()),
        std_item=std_item,
        value=value,
        period_end=period,
        period_type=ptype,
        fiscal_year=period[:4],
        currency=kw.pop("currency", "CNY"),
        **kw,
    )


class TestSafeDiv(unittest.TestCase):
    def test_zero_denominator(self):
        self.assertIsNone(safe_div(100.0, 0.0))

    def test_missing_values(self):
        self.assertIsNone(safe_div(None, 10.0))
        self.assertIsNone(safe_div(10.0, None))

    def test_normal(self):
        self.assertAlmostEqual(safe_div(50.0, 200.0), 0.25)

    def test_negative_denominator_allowed(self):
        # 财务费用为负是合法情形，不应被静默吞掉
        self.assertAlmostEqual(safe_div(100.0, -50.0), -2.0)


class TestGrowth(unittest.TestCase):
    def test_negative_base_returns_none(self):
        self.assertIsNone(growth(100.0, -50.0))

    def test_current_negative_returns_none(self):
        self.assertIsNone(growth(-100.0, 50.0))

    def test_near_zero_base(self):
        self.assertIsNone(growth(100.0, 1e-9))

    def test_normal_growth(self):
        self.assertAlmostEqual(growth(150.0, 100.0), 0.5)

    def test_decline(self):
        self.assertAlmostEqual(growth(50.0, 100.0), -0.5)


class TestFactSet(unittest.TestCase):
    def setUp(self):
        self.facts = FactSet(
            [
                fact("revenue", "2026-06-30", 1000.0, PeriodType.INTERIM),
                fact("revenue", "2025-06-30", 800.0, PeriodType.INTERIM),
                fact("revenue", "2025-12-31", 2000.0, PeriodType.ANNUAL),
                fact("revenue", "2026-03-31", 400.0, PeriodType.Q1),
            ]
        )

    def test_series_same_period_type(self):
        interim = self.facts.series("revenue", PeriodType.INTERIM)
        self.assertEqual([p.period_end for p in interim], ["2026-06-30", "2025-06-30"])

    def test_latest_is_most_recent(self):
        self.assertEqual(self.facts.latest("revenue").period_end, "2026-06-30")

    def test_periods_filtered_by_type(self):
        self.assertEqual(self.facts.periods(PeriodType.ANNUAL), ["2025-12-31"])

    def test_half_year_not_mixed_with_annual(self):
        """中报不得与年报口径混用做同比。"""
        interim = {p.period_end: p.value for p in self.facts.series("revenue", PeriodType.INTERIM)}
        self.assertNotIn("2025-12-31", interim)

    def test_currency_preserved(self):
        facts = FactSet([fact("revenue", "2026-06-30", 100.0, currency="HKD")])
        self.assertEqual(facts.latest("revenue").currency, "HKD")

    def test_error_facts_excluded(self):
        err = fact("__error__", "", None)
        err.note = "获取失败"
        facts = FactSet([err, fact("revenue", "2026-06-30", 1.0)])
        self.assertNotIn("__error__", facts.available_items())
        self.assertEqual(len(facts.errors()), 1)
        self.assertIn("获取失败", facts.errors()[0])

    def test_error_fact_value_not_indexed(self):
        err = fact("__error__", "", None)
        err.note = "获取失败"
        fs = FactSet([err, fact("revenue", "2026-06-30", 1.0)])
        self.assertIsNone(fs.value("__error__", "2026-06-30"))


class TestMateriality(unittest.TestCase):
    """重要性判断：科目占比过低时增速指标不形成结论。"""

    def test_immaterial_inventory(self):
        facts = FactSet(
            [
                fact("inventory", "2026-06-30", 6.85e8, PeriodType.INTERIM,
                     statement=Statement.BALANCE),
                fact("total_assets", "2026-06-30", 2.16e12, PeriodType.INTERIM,
                     statement=Statement.BALANCE),
            ]
        )
        ratio = facts.value("inventory", "2026-06-30") / facts.value("total_assets", "2026-06-30")
        self.assertLess(ratio, 0.02)

    def test_material_inventory(self):
        facts = FactSet(
            [
                fact("inventory", "2026-06-30", 5e10, PeriodType.INTERIM,
                     statement=Statement.BALANCE),
                fact("total_assets", "2026-06-30", 1e11, PeriodType.INTERIM,
                     statement=Statement.BALANCE),
            ]
        )
        ratio = facts.value("inventory", "2026-06-30") / facts.value("total_assets", "2026-06-30")
        self.assertGreaterEqual(ratio, 0.02)


class TestAuditOpinionScan(unittest.TestCase):
    """审计意见识别必须排除模板化表述。"""

    def test_checkbox_boilerplate_not_flagged(self):
        doc = ParsedDoc(
            "t1", 1,
            [(1, "五、上年年度报告非标准审计意见涉及事项的变化及处理情况 □适用 √不适用")],
            False,
        )
        self.assertEqual(scan_audit_opinions(doc), [])

    def test_standard_going_concern_boilerplate_not_flagged(self):
        doc = ParsedDoc(
            "t2", 1,
            [(1, "然而，未来的事项或情况可能导致该公司不能持续经营。")],
            False,
        )
        self.assertEqual(scan_audit_opinions(doc), [])

    def test_affirmative_qualified_opinion_flagged(self):
        doc = ParsedDoc(
            "t3", 1,
            [(1, "我们认为，除本报告所述事项外，我们对财务报表出具了保留意见。")],
            False,
        )
        hits = scan_audit_opinions(doc)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["label"], "非标准审计意见")
        self.assertEqual(hits[0]["page"], 1)

    def test_going_concern_uncertainty_flagged(self):
        doc = ParsedDoc(
            "t4", 1,
            [(1, "这些事项表明存在可能导致对公司持续经营能力产生重大疑虑的重大不确定性。")],
            False,
        )
        hits = scan_audit_opinions(doc)
        self.assertTrue(any(h["label"] == "持续经营重大不确定性" for h in hits))

    def test_disclaimer_of_opinion_flagged(self):
        doc = ParsedDoc(
            "t5", 1, [(1, "由于上述事项的重要性，我们无法表示意见。")], False
        )
        hits = scan_audit_opinions(doc)
        self.assertTrue(any(h["severity"] == "high" for h in hits))


class TestEvidenceVerification(unittest.TestCase):
    def test_quote_present_in_claimed_page(self):
        doc = ParsedDoc("d1", 1, [(3, "本公司应收账款余额为 12,345 万元")], False)
        ev = Evidence(
            evidence_id="d1:p3", doc_id="d1", title="测试公告",
            quote="本公司应收账款余额为 12,345 万元", location="第 3 页",
            url="https://example.com/a.pdf", source="test",
        )
        verify_evidence(ev, doc)
        self.assertTrue(ev.verified)

    def test_quote_absent_from_claimed_page(self):
        doc = ParsedDoc("d2", 2, [(1, "第一页内容"), (2, "第二页内容")], False)
        ev = Evidence(
            evidence_id="d2:p2", doc_id="d2", title="测试公告",
            quote="这段内容根本不存在", location="第 2 页",
        )
        verify_evidence(ev, doc)
        self.assertFalse(ev.verified)

    def test_fingerprint_stable(self):
        a = Evidence.fingerprint_of("  同一  段  文字  ")
        b = Evidence.fingerprint_of("同一段文字")
        self.assertEqual(a, b)


class TestUrlSafety(unittest.TestCase):
    def test_rejects_javascript(self):
        self.assertEqual(safe_url("javascript:alert(1)"), "")

    def test_allows_https(self):
        self.assertEqual(safe_url("https://www.cninfo.com.cn/a.pdf"),
                         "https://www.cninfo.com.cn/a.pdf")

    def test_rejects_file(self):
        self.assertEqual(safe_url("file:///etc/passwd"), "")


class TestIndustryClassification(unittest.TestCase):
    def test_bank(self):
        self.assertEqual(classify_industry("金融业-货币金融服务", "平安银行"), "bank")

    def test_realestate(self):
        self.assertEqual(classify_industry("房地产业-房地产业", "万科A"), "realestate")

    def test_general(self):
        self.assertEqual(classify_industry("制造业-酒、饮料和精制茶制造业", "贵州茅台"), "general")

    def test_insurance(self):
        self.assertEqual(classify_industry("金融业-保险业", "中国平安"), "insurance")


class TestTextUtils(unittest.TestCase):
    def test_traditional_conversion(self):
        self.assertEqual(to_traditional("腾讯控股"), "騰訊控股")

    def test_evidence_keywords_include_both_forms(self):
        kws = evidence_keywords("腾讯控股", "00700", Market.HK)
        self.assertIn("騰訊控股", kws)
        self.assertIn("00700", kws)
        self.assertIn("風險", kws)

    def test_a_share_keywords_simplified(self):
        kws = evidence_keywords("贵州茅台", "600519", Market.A)
        self.assertIn("风险", kws)


class TestRuleStatusCoverage(unittest.TestCase):
    """规则输出必须限定在五种结论之内。"""

    def test_status_values(self):
        from app.core.models import RuleStatus

        allowed = {
            "发现风险", "需要关注", "已覆盖资料中未发现明显异常",
            "数据不足，无法判断", "不适用",
        }
        self.assertEqual({s.value for s in RuleStatus}, allowed)

    def test_registry_size(self):
        from app.engine.runner import build_registry

        registry = build_registry()
        # 方案要求首版 30—40 项检查（通用）+ 行业包
        self.assertGreaterEqual(len(registry), 30)
        general = [r for r in registry.rules if "general" in r.packs]
        self.assertGreaterEqual(len(general), 30)

    def test_rule_ids_unique(self):
        from app.engine.runner import build_registry

        ids = [r.rule_id for r in build_registry().rules]
        self.assertEqual(len(ids), len(set(ids)))

    def test_industry_exclusions(self):
        """银行不应触发资产负债率等普通企业财务结构规则。"""
        from app.engine.runner import build_registry

        registry = build_registry()
        for rid in ("SV02", "SV03", "SV04"):
            rule = next(r for r in registry.rules if r.rule_id == rid)
            self.assertIn("bank", rule.exclude_packs, f"{rid} 应对银行不适用")

    def test_realestate_uses_special_leverage_rule(self):
        from app.engine.runner import build_registry

        registry = build_registry()
        sv02 = next(r for r in registry.rules if r.rule_id == "SV02")
        re01 = next(r for r in registry.rules if r.rule_id == "RE01")
        self.assertIn("realestate", sv02.exclude_packs)
        self.assertIn("realestate", re01.packs)


def _dims(*results):
    return [{"dimension": "财务质量", "results": list(results)}]


def _r(status, sev=None):
    return {"status": status, "severity": sev, "rule_id": "T00", "name": "测试规则"}


class TestRiskScore(unittest.TestCase):
    """风险信号评分：扣分加权、评级分档、边界样本。"""

    def test_all_clean_scores_100_grade_a(self):
        s = risk_signal_score(
            _dims(_r("已覆盖资料中未发现明显异常"), _r("不适用"),
                  _r("数据不足，无法判断"))
        )
        self.assertEqual(s["score"], 100)
        self.assertEqual(s["grade"], "A")
        self.assertEqual(s["risk_deduction"], 0)
        self.assertEqual(s["watch_deduction"], 0)

    def test_six_high_risks_score_28_grade_e(self):
        s = risk_signal_score(_dims(*[_r("发现风险", "高") for _ in range(6)]))
        self.assertEqual(s["score"], 28)
        self.assertEqual(s["grade"], "E")

    def test_three_low_watch_scores_97_grade_a(self):
        s = risk_signal_score(_dims(*[_r("需要关注", "低") for _ in range(3)]))
        self.assertEqual(s["score"], 97)
        self.assertEqual(s["grade"], "A")

    def test_insufficient_and_na_no_deduction(self):
        s = risk_signal_score(
            _dims(_r("数据不足，无法判断", "高"), _r("不适用", "高"))
        )
        self.assertEqual(s["score"], 100)

    def test_score_floor_zero(self):
        s = risk_signal_score(_dims(*[_r("发现风险", "高") for _ in range(20)]))
        self.assertEqual(s["score"], 0)
        self.assertEqual(s["grade"], "E")

    def test_mixed_deduction_math(self):
        # 高1(12) + 中2(16) + 低1(4) + 关注中1(2) + 关注低1(1) = 35 → 65 分 C
        s = risk_signal_score(
            _dims(_r("发现风险", "高"), _r("发现风险", "中"), _r("发现风险", "中"),
                  _r("发现风险", "低"), _r("需要关注", "中"), _r("需要关注", "低"))
        )
        self.assertEqual(s["risk_deduction"], 32)
        self.assertEqual(s["watch_deduction"], 3)
        self.assertEqual(s["score"], 65)
        self.assertEqual(s["grade"], "C")

    def test_unknown_severity_deducts_default(self):
        s = risk_signal_score(_dims(_r("发现风险", None)))
        self.assertEqual(s["risk_deduction"], 8)
        s2 = risk_signal_score(_dims(_r("需要关注", "未定")))
        self.assertEqual(s2["watch_deduction"], 1)

    def test_grade_boundaries(self):
        # 边界值精确落在分档阈值上
        self.assertEqual(risk_signal_score(_dims(_r("发现风险", "低"), _r("需要关注", "低")))["grade"], "A")  # 95
        self.assertEqual(risk_signal_score(_dims(_r("发现风险", "低"), _r("需要关注", "中")))["grade"], "B")  # 94
        self.assertEqual(risk_signal_score(_dims(*[_r("发现风险", "低") for _ in range(5)]))["grade"], "B")  # 80
        self.assertEqual(risk_signal_score(_dims(*[_r("发现风险", "中") for _ in range(5)]))["grade"], "C")  # 60
        self.assertEqual(risk_signal_score(_dims(*[_r("发现风险", "高") for _ in range(6)]))["grade"], "E")  # 28
        self.assertEqual(risk_signal_score(_dims(*[_r("发现风险", "高") for _ in range(5)]))["grade"], "D")  # 40

    def test_comp_string(self):
        s = risk_signal_score(_dims(_r("发现风险", "高"), _r("发现风险", "低"), _r("需要关注", "中")))
        self.assertEqual(s["risk_comp"], "高 1×12 ＋ 低 1×4")
        self.assertEqual(s["watch_comp"], "中 1×2")

    def test_empty_dimensions(self):
        s = risk_signal_score([])
        self.assertEqual((s["score"], s["grade"]), (100, "A"))
        s2 = risk_signal_score(None)
        self.assertEqual((s2["score"], s2["grade"]), (100, "A"))


class TestReportRendering(unittest.TestCase):
    """报告渲染：转义、缺字段安全、离线自包含。"""

    def setUp(self):
        self.payload = self._payload()

    @staticmethod
    def _payload():
        return {
            "report_version": "1.0", "rule_version": "1.0", "task_id": "testtask",
            "generated_at": "2026-09-05T00:00:00",
            "security": {"code": "600519", "market": "A", "name": "贵州茅台",
                         "secucode": "600519.SH", "org_name": "贵州茅台酒股份有限公司",
                         "exchange": "上海证券交易所", "currency": "CNY",
                         "industry": "白酒", "profile": {}},
            "company": None, "industry_pack": "general",
            "scan": {"started_at": "2026-09-05", "elapsed_seconds": 12.3,
                     "timed_out": False, "status": "完成"},
            "data_scope": {"fiscal_periods": ["2026-06-30"], "latest_period": "2026-06-30",
                           "latest_period_label": "2026年中报", "announcement_range": "x~y",
                           "announcement_total": 10, "announcement_fetched": 10,
                           "documents_downloaded": 3, "documents_parsed": 3,
                           "evidence_count": 5, "evidence_verified": 5,
                           "currencies": ["CNY"], "statements": ["income"], "source": "cninfo"},
            "summary": {"highest_severity": "高", "risk_count": 1, "watch_count": 0,
                        "insufficient_count": 1, "top_findings": [],
                        "coverage": {"applicable": 10, "evaluated": 8, "insufficient": 2,
                                     "not_applicable": 0, "by_dimension": {}}},
            "metrics": {"currency": "CNY", "latest_period": "2026-06-30",
                        "prior_period": "2025-06-30", "items": {}},
            "trends": {"revenue": [{"period": "2026-06-30", "label": "2026年中报", "value": 1e10}]},
            "dimensions": [], "timeline": [], "mitigations": [], "gaps": ["测试缺口"],
            "missing_data": [], "evidence": {}, "documents": [],
            "ai": {"usage": {"available": False, "reason": "未配置"}, "notes": [],
                   "verification": []},
            "method": {"network": {}, "limitations": ["限制1"],
                       "disclaimer": "本报告不构成投资建议"},
            "plan": {"公告区间": "2025-09-05 ~ 2026-09-05"},
        }

    def test_renders(self):
        from app.report.render import render_inline

        html = render_inline(self.payload)
        # 标题格式：短名（代码）排雷报告
        self.assertIn("贵州茅台（600519.SH）排雷报告", html)
        self.assertIn("本报告不构成投资建议", html)

    def test_escapes_injection(self):
        from app.report.render import render_inline

        payload = self._payload()
        payload["security"]["name"] = "<script>alert(1)</script>"
        html = render_inline(payload)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_no_external_resources(self):
        import re

        from app.report.render import render_inline

        html = render_inline(self.payload)
        # 禁止外链资源加载（内联脚本/CSS/SVG 与 <a> 来源链接允许，保证离线自包含）
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("http://fonts", html)
        self.assertNotIn("@import", html)
        # <img> 只允许 data: 内嵌（当前模板无 img；防回归）
        for m in re.finditer(r"<img[^>]*src=\"([^\"]*)\"", html):
            self.assertTrue(m.group(1).startswith("data:"), f"外链图片: {m.group(1)}")

    def test_risk_score_block_renders(self):
        from app.report.render import render_inline

        payload = self._payload()
        payload["dimensions"] = [
            {"dimension": "财务质量", "results": [
                {"status": "发现风险", "severity": "中", "rule_id": "FQ01", "name": "测试规则"},
            ]},
        ]
        html = render_inline(payload)
        self.assertIn("风险信号评分", html)
        self.assertIn("g-b", html)  # 100-8=92 → B
        self.assertNotIn("风险等级分布", html)

    def test_chart_renders_inline_svg(self):
        from app.report.charts import trend_chart

        svg = trend_chart(
            [{"period": "2025-06-30", "label": "2025年中报", "value": 100.0},
             {"period": "2026-06-30", "label": "2026年中报", "value": -50.0}],
            "净利润",
        )
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)

    def test_chart_handles_empty(self):
        from app.report.charts import trend_chart

        self.assertIn("无可用数据", trend_chart([], "净利润"))

    def test_chart_handles_negatives(self):
        from app.report.charts import trend_chart

        svg = trend_chart(
            [{"period": "2026-06-30", "label": "2026年中报", "value": -100.0}], "净利润"
        )
        self.assertIn("<svg", svg)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
