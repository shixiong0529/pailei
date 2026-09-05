"""财务事实标准化与时间序列组织。

对应方案 §5：
- 同期比较必须使用一致口径（年报对年报、中报对中报）；
- 不把半年度数据直接当作全年数据；
- 原值与标准化结果分开保存；
- 受限资金不视同可自由偿债现金。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.core.models import FinancialFact, PeriodType, Statement


@dataclass
class Point:
    """某个标准科目在特定报告期上的取值及其来源。"""

    period_end: str
    period_type: PeriodType
    value: float
    currency: str
    notice_date: str
    source_id: str
    raw_item: str

    @property
    def label(self) -> str:
        return f"{self.period_end[:4]}年{self.period_type.label}"


@dataclass
class FactSet:
    """按标准科目组织的财务事实集合。"""

    facts: list[FinancialFact] = field(default_factory=list)
    _index: dict[tuple[str, str], FinancialFact] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for f in self.facts:
            if f.std_item == "__error__" or f.value is None:
                continue
            if not f.period_end:
                continue
            self._index[(f.std_item, f.period_end)] = f

    def add(self, facts: Iterable[FinancialFact]) -> None:
        for f in facts:
            self.facts.append(f)
            if f.std_item == "__error__" or f.value is None or not f.period_end:
                continue
            self._index[(f.std_item, f.period_end)] = f

    def get(self, std_item: str, period_end: str) -> Optional[FinancialFact]:
        return self._index.get((std_item, period_end))

    def value(self, std_item: str, period_end: str) -> Optional[float]:
        f = self.get(std_item, period_end)
        return f.value if f else None

    def periods(self, period_type: PeriodType | None = None) -> list[str]:
        got = {f.period_end for f in self.facts if f.period_end and f.value is not None}
        if period_type:
            got = {
                f.period_end
                for f in self.facts
                if f.period_end and f.value is not None and f.period_type is period_type
            }
        return sorted(got, reverse=True)

    def series(
        self, std_item: str, period_type: PeriodType | None = None, limit: int = 8
    ) -> list[Point]:
        """取某科目的时间序列，默认只返回同一期次口径（保证可比）。"""
        points: list[Point] = []
        for f in self.facts:
            if f.std_item != std_item or f.value is None or not f.period_end:
                continue
            if period_type and f.period_type is not period_type:
                continue
            points.append(
                Point(
                    period_end=f.period_end,
                    period_type=f.period_type,
                    value=f.value,
                    currency=f.currency,
                    notice_date=f.notice_date,
                    source_id=f.source_id,
                    raw_item=f.raw_item,
                )
            )
        # 同一报告期可能有多条（不同报表），取最新抓取的一条
        dedup: dict[tuple[str, str], Point] = {}
        for p in points:
            dedup[(p.period_end, p.period_type.value)] = p
        ordered = sorted(dedup.values(), key=lambda p: p.period_end, reverse=True)
        return ordered[:limit]

    def latest(self, std_item: str, period_type: PeriodType | None = None) -> Optional[Point]:
        s = self.series(std_item, period_type, limit=1)
        return s[0] if s else None

    def latest_period(self) -> str:
        periods = self.periods()
        return periods[0] if periods else ""

    def latest_period_type(self) -> Optional[PeriodType]:
        period = self.latest_period()
        for f in self.facts:
            if f.period_end == period:
                return f.period_type
        return None

    def available_items(self) -> list[str]:
        return sorted({f.std_item for f in self.facts if f.std_item != "__error__"})

    def currencies(self) -> list[str]:
        return sorted({f.currency for f in self.facts if f.currency})

    def statements_present(self) -> list[str]:
        return sorted({f.statement.value for f in self.facts if f.std_item != "__error__"})

    def errors(self) -> list[str]:
        return [f.note for f in self.facts if f.std_item == "__error__"]

    def coverage_note(self) -> dict[str, object]:
        return {
            "fact_count": len([f for f in self.facts if f.std_item != "__error__"]),
            "periods": self.periods(),
            "items": self.available_items(),
            "statements": self.statements_present(),
            "currencies": self.currencies(),
            "errors": self.errors(),
        }


# ---------------------------------------------------------------- 计算工具


def safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """安全除法：分母缺失、为零或分子缺失时返回 None，绝不产出 inf/nan。"""
    if a is None or b is None:
        return None
    if b == 0:
        return None
    try:
        result = a / b
    except (ZeroDivisionError, TypeError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def growth(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """同比增长率。基数为负或接近零时不套用普通同比解释，返回 None。"""
    if current is None or previous is None:
        return None
    if abs(previous) < 1e-6:
        return None
    if previous < 0 or current < 0:
        # 负利润基数下的同比无经济含义，交由调用方另行说明
        return None
    return (current - previous) / abs(previous)


def pct(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def money(value: Optional[float], unit: str = "亿元") -> str:
    if value is None:
        return "—"
    scaling = {"元": 1, "万元": 1e4, "亿元": 1e8, "万亿": 1e12}
    divisor = scaling.get(unit, 1e8)
    return f"{value / divisor:,.2f} {unit}"


def is_annual_only(item_period_type: PeriodType) -> bool:
    return item_period_type is PeriodType.ANNUAL
