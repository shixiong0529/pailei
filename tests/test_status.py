"""V1.2 阶段 4 测试：任务状态、覆盖等级与评分表达。

全离线、确定性。覆盖：
- 任务状态与覆盖程度分离（生成成功/超时/生成失败/排队/运行）；
- 旧状态（完成/部分完成/失败）展示向后兼容；
- 缺口结构化严重度：关键缺口 vs 一般缺口，覆盖等级推导；
- 报告移除 A—E 公司等级，首页改为展示关键指标（最高已确认风险、风险/关注、覆盖率、证据复核率）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.models import (  # noqa: E402
    CoverageLevel,
    TaskStatus,
    display_status,
    task_is_done,
)
from app.engine.gaps import GapSeverity, classify_gap, coverage_level_of  # noqa: E402


class TaskStatusContractTests(unittest.TestCase):
    def test_status_has_required_states(self):
        values = {s.value for s in TaskStatus}
        self.assertIn("排队", values)
        self.assertIn("运行", values)
        self.assertIn("生成成功", values)
        self.assertIn("生成失败", values)
        self.assertIn("超时", values)
        self.assertNotIn("部分完成", values, "覆盖程度应从任务状态中剥离")

    def test_coverage_levels(self):
        self.assertEqual(CoverageLevel.COMPLETE.value, "完整")
        self.assertEqual(CoverageLevel.MINOR.value, "一般缺口")
        self.assertEqual(CoverageLevel.CRITICAL.value, "关键缺口")

    def test_display_status_legacy_compat(self):
        self.assertEqual(display_status("完成"), "生成成功")
        self.assertEqual(display_status("部分完成"), "生成成功")
        self.assertEqual(display_status("失败"), "生成失败")
        self.assertEqual(display_status("生成成功"), "生成成功")
        self.assertEqual(display_status("超时"), "超时")
        self.assertEqual(display_status(""), "")

    def test_task_is_done(self):
        self.assertTrue(task_is_done("生成成功"))
        self.assertTrue(task_is_done("超时"))
        self.assertTrue(task_is_done("完成"))       # 旧状态
        self.assertTrue(task_is_done("部分完成"))   # 旧状态
        self.assertFalse(task_is_done("运行"))
        self.assertFalse(task_is_done("排队"))
        self.assertFalse(task_is_done("生成失败"))


class GapClassificationTests(unittest.TestCase):
    def test_critical_gaps(self):
        for msg in ("财务数据获取未完成：连接失败",
                    "未获取到任何财务报告期数据，全部财务类检查项将判定为数据不足",
                    "公告获取未完成：连接失败",
                    "任务期限已到，公告获取未执行"):
            self.assertIs(classify_gap(msg), GapSeverity.CRITICAL, msg)

    def test_minor_gaps(self):
        for msg in ("《年报》仅解析 120/300 页，剩余正文未覆盖",
                    "模型步骤未完整执行：解读超时",
                    "12 份公告中仅下载最多 5 份原文，其余仅检查标题"):
            self.assertIs(classify_gap(msg), GapSeverity.MINOR, msg)

    def test_coverage_level_complete(self):
        self.assertEqual(coverage_level_of([]), "完整")

    def test_coverage_level_minor(self):
        self.assertEqual(coverage_level_of(["《年报》仅解析 120/300 页"]), "一般缺口")

    def test_coverage_level_critical_dominates(self):
        gaps = ["《年报》仅解析 120/300 页", "未获取到任何财务报告期数据"]
        self.assertEqual(coverage_level_of(gaps), "关键缺口")


class ReportStatusExpressionTests(unittest.TestCase):
    def test_report_keeps_user_selected_grade_and_shows_metrics(self):
        from app.report.render import render_inline
        from tests.test_validation import _base_payload

        p = _base_payload()
        p["scan"]["status"] = "生成成功"
        p["scan"]["coverage_level"] = "一般缺口"
        p["data_scope"]["evidence_count"] = 50
        p["data_scope"]["evidence_verified"] = 45
        html = render_inline(p)

        self.assertIn('class="badge grade grade-', html)  # 用户选择保留醒目的 A—E 风险等级
        self.assertIn("风险信号密度", html)            # 0-100 降为次要指标
        self.assertIn("最高已确认风险", html)
        self.assertIn("风险 / 关注", html)
        self.assertIn("有效覆盖率", html)
        self.assertIn("证据复核率", html)
        self.assertIn("90.0%", html)                  # 45/50 = 90%
        self.assertIn("一般缺口", html)

    def test_report_without_coverage_level_still_renders(self):
        from app.report.render import render_inline
        from tests.test_validation import _base_payload

        p = _base_payload()
        html = render_inline(p)
        self.assertIn('class="badge grade grade-', html)
        self.assertIn("覆盖等级", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
