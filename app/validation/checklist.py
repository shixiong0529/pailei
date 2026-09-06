"""固定人工标注验收集的加载与结构校验。

验收集用于可重复的验收：每家公司记录证券身份、数据快照、预期触发、禁止触发、
证据位置、是否已解除、标注人、标注状态。尚未人工确认的条目标记 pending_review，
不算作通过样本，也不得编造人工标签。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CHECKLIST_PATH = Path(__file__).parent / "checklist.json"

REQUIRED_FIELDS = {
    "secucode", "code", "market", "name", "industry", "industry_pack",
    "scenario_hint", "data_snapshot", "expected_triggers", "forbidden_triggers",
    "evidence_location", "resolved", "annotator", "review_status",
}

VALID_MARKETS = {"A", "HK"}
VALID_PACKS = {"general", "bank", "insurance", "broker", "realestate"}
VALID_REVIEW_STATUS = {"pending_review", "baseline", "reviewed"}


def load_checklist() -> dict[str, Any]:
    """加载验收集（只读）。"""
    return json.loads(CHECKLIST_PATH.read_text(encoding="utf-8"))


def validate_checklist(data: dict[str, Any] | None = None) -> list[str]:
    """返回结构问题清单；空列表表示通过。

    校验规则：
    - 不少于 20 家，覆盖 A/H 两个市场；
    - 覆盖 general/bank/insurance/broker/realestate 五个行业包；
    - 每条目字段完整，market/industry_pack/review_status 取值合法；
    - pending_review 条目不得出现编造的人工标签（预期/禁止触发、证据位置、标注人必须为空）；
    - 至少保留三个规则 1.1 基准样本（review_status=baseline）。
    """
    errors: list[str] = []
    data = data if data is not None else load_checklist()
    entries = data.get("entries")
    if not isinstance(entries, list):
        return ["entries 必须是数组"]
    if len(entries) < 20:
        errors.append(f"验收集需不少于 20 家，当前 {len(entries)} 家")

    markets = {e.get("market") for e in entries}
    if not ({"A", "HK"} <= markets):
        errors.append(f"验收集需覆盖 A 与 HK 两个市场，当前 {sorted(markets)}")

    packs = {e.get("industry_pack") for e in entries}
    missing_packs = VALID_PACKS - packs
    if missing_packs:
        errors.append(f"验收集需覆盖全部行业包，缺少 {sorted(missing_packs)}")

    secucodes = [e.get("secucode") for e in entries]
    if len(secucodes) != len(set(secucodes)):
        errors.append("验收集存在重复的 secucode")

    baselines = 0
    for i, e in enumerate(entries):
        prefix = f"entries[{i}]"
        missing = REQUIRED_FIELDS - set(e.keys())
        if missing:
            errors.append(f"{prefix} 缺少字段 {sorted(missing)}")
        if e.get("market") not in VALID_MARKETS:
            errors.append(f"{prefix} market 取值非法：{e.get('market')}")
        if e.get("industry_pack") not in VALID_PACKS:
            errors.append(f"{prefix} industry_pack 取值非法：{e.get('industry_pack')}")
        status = e.get("review_status")
        if status not in VALID_REVIEW_STATUS:
            errors.append(f"{prefix} review_status 取值非法：{status}")
        if status == "baseline":
            baselines += 1
            if not e.get("baseline_summary"):
                errors.append(f"{prefix} 基准样本缺少 baseline_summary")
        if status == "pending_review":
            # 尚未人工确认，不得编造标签
            if e.get("expected_triggers") or e.get("forbidden_triggers"):
                errors.append(f"{prefix} pending_review 条目不得编造预期/禁止触发标签")
            if e.get("evidence_location") or e.get("annotator"):
                errors.append(f"{prefix} pending_review 条目不得填写证据位置或标注人")
    if baselines < 3:
        errors.append(f"需保留至少 3 个规则 1.1 基准样本，当前 {baselines} 个")
    return errors


def baseline_entries() -> list[dict[str, Any]]:
    """返回带规则 1.1 基准摘要的样本。"""
    return [e for e in load_checklist()["entries"] if e.get("review_status") == "baseline"]
