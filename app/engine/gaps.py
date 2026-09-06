"""缺口分类：为运行缺口建立结构化严重度，区分关键缺口与一般缺口。

V1.2 阶段 4：报告覆盖程度不再用单一「部分完成」表达，而是
- 关键缺口：缺少核心财务报表、公告清单获取失败等，会削弱整体结论可信度；
- 一般缺口：文件页数超上限、单份原文获取失败、模型步骤未完整执行等；
- 说明：计算口径等说明性文字不是缺口（在流水线中已归入 notes，不进入 gaps）。
"""

from __future__ import annotations

from enum import Enum

from app.core.models import CoverageLevel


class GapSeverity(str, Enum):
    CRITICAL = "关键"
    MINOR = "一般"


# 关键缺口关键词：核心财务数据缺失 / 公告清单失败 / 主体定位失败。
CRITICAL_HINTS = (
    "未获取到任何财务报告期数据",
    "财务数据获取未完成",
    "停止财务数据请求",
    "公告获取未完成",
    "公告获取未执行",
    "无法在巨潮定位",
    "无法在披露易定位",
)


def classify_gap(message: str) -> GapSeverity:
    """按缺口文本判定严重度。默认按一般缺口处理（宁可低估也不夸大关键性）。"""
    msg = message or ""
    for hint in CRITICAL_HINTS:
        if hint in msg:
            return GapSeverity.CRITICAL
    return GapSeverity.MINOR


def coverage_level_of(gaps: list[str]) -> str:
    """由缺口列表推导覆盖等级：存在关键缺口 → 关键缺口；仅有一般缺口 → 一般缺口；否则完整。"""
    has_minor = False
    for gap in gaps or []:
        if classify_gap(gap) is GapSeverity.CRITICAL:
            return CoverageLevel.CRITICAL.value
        has_minor = True
    return CoverageLevel.MINOR.value if has_minor else CoverageLevel.COMPLETE.value
