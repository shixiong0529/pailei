"""事件生命周期与按需历史追溯。

把同一事项的多份披露串成生命周期：
    首次发生 → 补充说明 → 回复/整改 → 最新进展 → 已解除或仍未解除

设计约束：
- 关联优先使用确定性字段（案件号、公告编号、标题关键词），模型只能辅助提出候选；
- “已解除”必须由后续正式披露支持，不能仅凭时间经过或模型判断；
- 同一事件的多份公告合并为一个生命周期，不重复计数；
- 历史追溯只在发现未解除的重要事件时才按需启动，并受查询/公告/下载/耗时上限约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from app.core.models import RiskEvent

# 各事件类别的“解除/进展”关键词（用于在后续披露中寻找解除依据）。
RESOLUTION_HINTS: dict[str, tuple[str, ...]] = {
    "监管问询": ("回复", "回函", "答复", "回覆", "答覆"),
    "监管处罚": ("整改", "结案", "結案"),
    "监管调查": ("整改", "结案", "結案"),
    "诉讼": ("和解", "结案", "撤诉", "判决", "結案", "撤訴", "判決"),
    "资产冻结": ("解除冻结", "解除司法冻结", "解除凍結", "解除司法凍結"),
    "股权质押": ("解除质押", "解除質押"),
    "质押冻结": ("解除质押", "解除冻结", "解除司法冻结", "解除質押", "解除凍結", "解除司法凍結"),
    "审计机构": ("聘任", "续聘", "改聘", "續聘"),
    "盈利警告": ("年度报告", "业绩快报", "年度報告", "業績快報"),
    "上市地位": ("复牌", "復牌"),
    "财务更正": ("更正",),
}

# 需要历史追溯的重要事件类别（未解除时）。
TRACEABLE_CATEGORIES = {
    "监管处罚", "监管调查", "诉讼", "资产冻结", "股权质押", "监管问询", "上市地位",
}

_CASE_RE = re.compile(r"[（(](\d{4})[）)][^，。；\s]{2,30}?第?\d+号")
_ANN_RE = re.compile(r"(?:公告编号|编号|临)\s*[:：]?\s*([A-Za-z0-9\-]{3,20})")


def extract_case_number(title: str) -> str:
    """从标题提取案件号，如 (2026)粤0304民初123号。"""
    m = _CASE_RE.search(title or "")
    return m.group(0) if m else ""


def extract_announcement_number(title: str) -> str:
    """从标题提取公告编号。"""
    m = _ANN_RE.search(title or "")
    return m.group(1) if m else ""


def _core_keywords(title: str) -> str:
    """剥离常见包装词、括号、数字，得到标题的核心关键词。

    “的补充/的进展/的提示性”这类连接词连同“的”一并剥离，使补充、进展公告能
    与首次披露归为同一事项（无案件号/公告编号时使用）。
    """
    t = re.sub(r"[（(][^）)]*[）)]", "", title or "")
    # 解除公告与原事项应落入同一个确定性标题键，否则在缺少案件号/公告编号时，
    # “资产冻结”永远无法被后续“解除冻结”公告关闭。
    t = re.sub(r"(?:解除)?(?:司法)?冻结", "冻结", t)
    t = re.sub(r"(?:解除)?(?:司法)?凍結", "凍結", t)
    t = t.replace("解除质押", "质押").replace("解除質押", "質押")
    t = re.sub(r"(关于|的补充|的进展|的提示性|的公告|公告|补充|进展|提示性)", "", t)
    t = re.sub(r"\d+", "", t)
    t = re.sub(r"[\s，。；：、\-—]", "", t)
    return t or (title or "")


def dedup_key(title: str) -> str:
    """同一事项的确定性关联键。

    优先级：案件号 > 公告编号 > 标题核心关键词。
    """
    case = extract_case_number(title)
    if case:
        return f"case:{case}"
    ann = extract_announcement_number(title)
    if ann:
        return f"ann:{ann}"
    return f"title:{_core_keywords(title)}"


def _is_resolution_title(doc_title: str, category: str) -> bool:
    hints = RESOLUTION_HINTS.get(category, ())
    return any(h in (doc_title or "") for h in hints)


def detect_resolution(event: RiskEvent, docs: list[Any]) -> tuple[bool, str, str]:
    """判断事件是否已解除，返回 (resolved, basis, date)。

    仅当存在日期晚于事件、且确定性关联键一致、且标题命中该类别解除关键词的后续披露时，
    才判定为已解除。时间经过或模型判断不构成解除依据。
    """
    key = dedup_key(event.title)
    best: Optional[tuple[str, str]] = None
    for doc in docs:
        title = getattr(doc, "title", "") or ""
        pub = getattr(doc, "publish_date", "") or ""
        # 公告不能把自己当作后续解除依据；发布日期只有天级精度，因此要求严格晚于事件日，
        # 避免同日的原始公告或重复记录被误判为“后续披露”。
        if (getattr(doc, "doc_id", "") or "") == (event.source_doc_id or ""):
            continue
        if not pub or pub <= (event.occurred_date or ""):
            continue
        if dedup_key(title) != key:
            continue
        if not _is_resolution_title(title, event.category):
            continue
        if best is None or pub > best[1]:
            best = (title, pub)
    if best:
        return True, best[0], best[1]
    return False, "", ""


def enrich_events(events: list[RiskEvent], docs: list[Any]) -> list[RiskEvent]:
    """为事件补充生命周期字段：关联键、阶段、解除状态与依据、关联公告。

    不修改确定性事件的既有结论；仅补充可追溯的关联信息。
    """
    key_to_docs: dict[str, list[Any]] = {}
    for doc in docs:
        key_to_docs.setdefault(dedup_key(getattr(doc, "title", "") or ""), []).append(doc)

    # 输入时间线通常按日期倒序展示，生命周期序号必须独立按时间正序计算。
    # 否则最新公告会被错误标成“首次发生”，最早公告反而成为“最新进展”。
    grouped: dict[str, list[RiskEvent]] = {}
    for ev in events:
        key = dedup_key(ev.title)
        ev.dedup_key = key
        grouped.setdefault(key, []).append(ev)
    for group in grouped.values():
        for index, ev in enumerate(
            sorted(group, key=lambda item: (item.occurred_date or "", item.event_id)), start=1
        ):
            ev.occurrence_order = index
            ev.lifecycle_stage = "首次发生" if index == 1 else "最新进展"

    for ev in events:
        key = ev.dedup_key
        ev.related_doc_ids = sorted(
            {d.doc_id for d in key_to_docs.get(key, []) if getattr(d, "doc_id", "")}
        )
        resolved, basis, rdate = detect_resolution(ev, docs)
        if resolved:
            ev.resolved = True
            ev.resolution_basis = basis
            ev.resolution_date = rdate
            ev.lifecycle_stage = "已解除"
        else:
            # 已有未解除或无法判断的状态：仅当有后续披露但非解除时才标注进展。
            ev.resolved = ev.resolved if ev.resolved is not None else None
    return events


def build_lifecycles(events: list[RiskEvent]) -> list[dict[str, Any]]:
    """按关联键合并事件，每个生命周期只出现一次，不重复计数。"""
    groups: dict[str, list[RiskEvent]] = {}
    for ev in events:
        groups.setdefault(ev.dedup_key or dedup_key(ev.title), []).append(ev)

    out: list[dict[str, Any]] = []
    for key, group in groups.items():
        group = sorted(group, key=lambda e: e.occurred_date)
        first = group[0]
        latest = group[-1]
        resolved = any(e.resolved for e in group)
        basis = next((e.resolution_basis for e in group if e.resolution_basis), "")
        rdate = next((e.resolution_date for e in group if e.resolution_date), "")
        out.append(
            {
                "key": key,
                "category": first.category,
                "title": first.title,
                "first_occurred": first.occurred_date,
                "latest_date": latest.occurred_date,
                "announcement_count": len(group),
                "resolved": resolved,
                "resolution_basis": basis,
                "resolution_date": rdate,
                "related_doc_ids": sorted({d for e in group for d in e.related_doc_ids}),
                "event_ids": [e.event_id for e in group],
            }
        )
    return sorted(out, key=lambda x: x["first_occurred"], reverse=True)


def should_trace_back(events: list[RiskEvent]) -> bool:
    """是否需要对未解除的重要事件按需向前追溯。"""
    return any(
        e.category in TRACEABLE_CATEGORIES and e.resolved is not True
        for e in events
    )


@dataclass
class TraceBackLimits:
    """单事件历史追溯的资源上限。达到上限必须记录缺口。"""

    max_queries: int = 3
    max_announcements: int = 30
    max_downloads: int = 5
    max_seconds: float = 60.0


@dataclass
class TraceBackPlan:
    categories: list[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    limits: TraceBackLimits = field(default_factory=TraceBackLimits)

    @property
    def empty(self) -> bool:
        return not self.categories


def trace_back_plan(
    events: list[RiskEvent],
    *,
    end: date | None = None,
    max_years: int = 3,
    limits: TraceBackLimits | None = None,
) -> TraceBackPlan:
    """为未解除的重要事件规划按需向前追溯的范围（最多 max_years 年）。"""
    unresolved = {
        e.category for e in events
        if e.category in TRACEABLE_CATEGORIES and e.resolved is not True
    }
    if not unresolved:
        return TraceBackPlan(limits=limits or TraceBackLimits())
    end = end or date.today()
    start = end - timedelta(days=365 * max(1, max_years))
    return TraceBackPlan(
        categories=sorted(unresolved),
        start=start.isoformat(),
        end=end.isoformat(),
        limits=limits or TraceBackLimits(),
    )
