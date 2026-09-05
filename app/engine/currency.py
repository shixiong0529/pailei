"""对港股结构化金额的计价币种做原始披露交叉核验，不能用交易币种替代。"""
from __future__ import annotations
import re
from collections import defaultdict
from app.core.models import Statement

_CURRENCY = re.compile(r"(人民[币幣]|港[币幣元]|美元|美金|RMB|HK\$|US\$)[\s（）()]*(百[万萬]|千|[万萬]|million|thousand)?元?", re.I)
_ITEMS = {"revenue", "total_revenue", "operating_revenue", "net_profit", "net_profit_attributable", "total_assets", "ocf"}


def verify_reporting_currencies(facts, docs, parsed) -> list[str]:
    groups = defaultdict(list)
    for fact in facts:
        if fact.period_end:
            groups[fact.period_end].append(fact)
    missing = []
    for period, group in groups.items():
        candidates = set()
        sources = []
        values = {f.value for f in group if f.std_item in _ITEMS and f.value is not None and abs(f.value) > 10000}
        for doc in docs:
            pdoc = parsed.get(doc.doc_id)
            if not pdoc or pdoc.error or doc.doc_type not in {"年报", "半年报", "业绩", "季报"}:
                continue
            # 文件必须披露该年度，且同一币种金额页至少对上两个独立的关键数值。
            date_text = (doc.title + pdoc.full_text[:15000]).translate(str.maketrans("零〇一二三四五六七八九", "00123456789"))
            if period[:4] not in date_text:
                continue
            for _, text in pdoc.pages:
                for match in _CURRENCY.finditer(text):
                    unit = (match.group(2) or "").lower()
                    scale = {"百萬": 1e6, "百万": 1e6, "千": 1e3, "万": 1e4, "萬": 1e4, "": 1, "million": 1e6, "thousand": 1e3}[unit]
                    raw = match.group(1)
                    currency = "CNY" if raw.startswith("人民") or raw.upper() == "RMB" else "HKD" if raw.startswith("港") or raw.upper() == "HK$" else "USD"
                    numbers = re.sub(r"[,，]", "", text)
                    count = sum(any(re.search(r"(?<!\d)" + re.escape(form) + r"(?!\d)", numbers)
                                    for form in {f"{abs(value)/scale:.0f}", f"{abs(value)/scale:.2f}"})
                                for value in values)
                    if count >= 2:
                        candidates.add(currency)
                        sources.append(doc.doc_id)
        if len(candidates) == 1:
            currency = candidates.pop()
            for fact in group:
                fact.currency = currency
                fact.note += f"；计价币种与原文关键金额交叉核对：{','.join(sorted(set(sources)))}"
        else:
            missing.append(period)
            for fact in group:
                fact.currency = "未核实"
                if fact.unit == "元":
                    fact.verified = False
                    fact.note += "；报表计价币种未完成原文核实，不使用交易币种替代"
    return (["以下港股报告期的计价币种未完成原文核实，相关金额判断受限：" + "、".join(sorted(missing, reverse=True))] if missing else [])
