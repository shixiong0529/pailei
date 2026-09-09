"""Versioned scoring groups. Merge overlapping checks, never let a missing result offset risk."""

def select_scored_results(dimensions, deduplicate=False):
    rows = [r for dim in dimensions or [] for r in dim.get("results") or []]
    if not deduplicate:
        return rows, []
    families = {"FQ01": "利润现金匹配", "FQ02": "利润现金匹配", "FQ13": "利润现金匹配",
                "SV01": "短期现金覆盖", "RE02": "短期现金覆盖", "SV10": "短期现金覆盖"}
    groups = {}
    ungrouped = []
    for r in rows:
        if r.get("status") not in {"发现风险", "需要关注"}:
            ungrouped.append(r)
            continue
        rid = r.get("rule_id", "")
        family = families.get(rid)
        if rid == "OP02" or (rid == "OP05" and "审计报告披露持续经营" in r.get("finding", "")):
            family = "审计持续经营"
        # SV08 includes many guarantees. Merge with GV07 only with identical evidence sets.
        if not family:
            ungrouped.append(r)
            continue
        groups.setdefault(family, []).append(r)
    merged = []
    for family, members in groups.items():
        def weight(r):
            if r.get("status") == "发现风险":
                return {"高": 12, "中": 8, "低": 4}.get(r.get("severity"), 8)
            return {"中": 2, "低": 1}.get(r.get("severity"), 1)
        chosen = max(members, key=weight)
        ungrouped.append(chosen)
        if len(members) > 1:
            merged.append({"family": family, "kept": chosen.get("rule_id"),
                           "members": [r.get("rule_id") for r in members],
                           "deduction": weight(chosen)})
    return ungrouped, merged
