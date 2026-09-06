"""披露文件选择：带类别配额的选择，重要类别不被例行公告挤出。

V1.2 阶段 5：从单一全局排名改为带类别配额的选择——
- 财报/审计、处罚/问询、诉讼/冻结/担保、治理变化等重要类别各设配额；
- 同类例行公告设上限，避免大量例行公告挤占重要类别；
- 选择原因写入运行诊断，供报告与复验追溯。
"""

from __future__ import annotations

from typing import Any

from app.core.models import DisclosureDoc

# 类别 → 成员 doc_type（按优先级），配额为最多选择的份数。
GROUP_QUOTAS: list[tuple[str, tuple[str, ...], int]] = [
    ("财报与审计", ("年报", "半年报", "季报", "一季报", "三季报", "审计", "财务更正"), 10),
    ("处罚与问询", ("监管处罚", "监管调查", "监管问询", "上市地位"), 6),
    ("诉讼冻结担保", ("诉讼", "资产冻结", "担保", "股权质押", "质押冻结", "冻结解除", "质押解除"), 6),
    ("治理与关联", ("审计机构", "高管变动", "股东减持", "股东增持", "减持承诺", "关联交易", "盈利警告", "业绩预告"), 6),
]

# 例行公告类型：同类设上限，避免挤占重要类别。
ROUTINE_TYPES = ("其他公告",)

# 优先级补充排序：未落入上述类别的公告按此顺序补足剩余名额。
PRIORITY_ORDER = (
    "年报", "半年报", "审计", "财务更正", "监管处罚", "监管调查", "诉讼",
    "资产冻结", "监管问询", "上市地位", "审计机构", "股权质押", "关联交易",
    "高管变动", "盈利警告", "业绩预告", "季报", "担保", "股东减持",
)


def _priority(doc: DisclosureDoc) -> int:
    return PRIORITY_ORDER.index(doc.doc_type) if doc.doc_type in PRIORITY_ORDER else len(PRIORITY_ORDER)


def select_documents(
    docs: list[DisclosureDoc],
    *,
    max_total: int,
    routine_cap: int = 4,
) -> tuple[list[DisclosureDoc], list[str]]:
    """按类别配额选择待下载原文，返回 (selected, reasons)。

    - 重要类别按配额选择，同一类别内按发布日期新→旧；
    - 非例行公告按优先级补齐剩余名额；
    - 例行公告（其他公告）设同类上限，仅在仍有余量时补充，不挤占重要类别；
    - 总数不超过 max_total。
    """
    reasons: list[str] = []

    def by_date(seq: list[DisclosureDoc]) -> list[DisclosureDoc]:
        return sorted(seq, key=lambda d: d.publish_date, reverse=True)

    selected: list[DisclosureDoc] = []
    used: set[str] = set()

    # 1. 重要类别配额
    for group, types, quota in GROUP_QUOTAS:
        members = by_date([d for d in docs if d.doc_type in types])
        picked = members[:quota]
        if members and len(members) > quota:
            reasons.append(f"「{group}」共 {len(members)} 份，按配额选择 {quota} 份")
        selected.extend(picked)
        used.update(d.doc_id for d in picked)

    # 2. 非例行公告按优先级补齐
    non_routine = [d for d in docs if d.doc_id not in used and d.doc_type not in ROUTINE_TYPES]
    non_routine.sort(key=lambda d: (_priority(d), d.publish_date))
    for d in non_routine:
        if len(selected) >= max_total:
            break
        selected.append(d)
        used.add(d.doc_id)

    # 3. 例行公告同类设上限，仅在仍有余量时补充
    routine = by_date([d for d in docs if d.doc_id not in used and d.doc_type in ROUTINE_TYPES])
    picked_routine = routine[:routine_cap]
    if routine and len(routine) > routine_cap:
        reasons.append(f"例行公告共 {len(routine)} 份，同类设上限，仅选择 {routine_cap} 份")
    for d in picked_routine:
        if len(selected) >= max_total:
            break
        selected.append(d)
        used.add(d.doc_id)

    if len(docs) > max_total and len(selected) < len(docs):
        reasons.append(f"共 {len(docs)} 份公告，按配额选择 {len(selected)} 份待下载原文")

    return selected[:max_total], reasons
