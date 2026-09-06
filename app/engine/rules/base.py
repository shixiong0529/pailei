"""规则框架。

规则以配置方式维护（方案 §6），每条包含：适用行业、所需数据、触发条件、
严重程度、解释模板与版本。每个检查项只能输出五种结论之一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core.models import (
    Capability,
    Dimension,
    DisclosureDoc,
    Evidence,
    Market,
    RuleStatus,
    Security,
    Severity,
    EvidenceStrength,
)
from app.engine.metrics import MetricsBundle
from app.engine.normalize import FactSet
from app.data.pdftext import ParsedDoc

RULE_VERSION = "1.1"


@dataclass
class EvidenceStore:
    """证据库。只有真正定位到原文片段的条目才会进入。

    除默认的公司关键词证据外，还按主题（如“审计意见”“诉讼”）建立索引，
    供规则在输出结论时绑定可核验的原文位置。
    """

    items: dict[str, Evidence] = field(default_factory=dict)
    topics: dict[str, list[str]] = field(default_factory=dict)
    doc_types: dict[str, str] = field(default_factory=dict)

    def add(
        self,
        evidence: Evidence,
        *,
        topics: Optional[list[str]] = None,
        doc_type: str = "",
    ) -> str:
        self.items[evidence.evidence_id] = evidence
        if doc_type:
            self.doc_types[evidence.evidence_id] = doc_type
        for topic in topics or []:
            self.topics.setdefault(topic, [])
            if evidence.evidence_id not in self.topics[topic]:
                self.topics[topic].append(evidence.evidence_id)
        return evidence.evidence_id

    def get(self, evidence_id: str) -> Optional[Evidence]:
        return self.items.get(evidence_id)

    def ids(self) -> list[str]:
        return list(self.items)

    def by_topic(self, topic: str, limit: int = 2) -> list[str]:
        return self.topics.get(topic, [])[:limit]

    def by_doc_types(self, doc_types: list[str], limit: int = 2) -> list[str]:
        out: list[str] = []
        for eid, dtype in self.doc_types.items():
            if dtype in doc_types:
                out.append(eid)
            if len(out) >= limit:
                break
        return out

    def by_titles(self, keywords: list[str], limit: int = 2) -> list[str]:
        out: list[str] = []
        for ev in self.items.values():
            if any(k in ev.title for k in keywords):
                out.append(ev.evidence_id)
            if len(out) >= limit:
                break
        return out

    def has_any(self, keywords: list[str]) -> list[Evidence]:
        return [ev for ev in self.items.values() if any(k in ev.title for k in keywords)]


@dataclass
class RuleContext:
    """规则执行上下文：全部数据由程序获取，AI 不提供数值。"""

    security: Security
    market: Market
    facts: FactSet
    metrics: MetricsBundle
    docs: list[DisclosureDoc]
    parsed: dict[str, ParsedDoc]
    evidence: EvidenceStore
    industry_pack: str = "general"
    llm_enabled: bool = False
    data_gaps: list[str] = field(default_factory=list)
    # 规则在检查过程中精确定位到的原文片段：(doc, quote, location, topics)
    pending_evidence: list[tuple[DisclosureDoc, str, str, list[str]]] = field(
        default_factory=list
    )

    def current_fact(self, item: str):
        fact = self.facts.get(item, self.metrics.latest_period)
        if fact and fact.unit == "元" and (fact.currency in {"", "未核实"} or fact.currency != self.metrics.currency):
            return None
        return fact

    def docs_of_type(self, *types: str) -> list[DisclosureDoc]:
        return [d for d in self.docs if d.doc_type in types]

    def doc_titles(self, *types: str) -> list[str]:
        return [d.title for d in self.docs_of_type(*types)]

    def take_pending_evidence(self) -> list[tuple[DisclosureDoc, str, str, list[str]]]:
        items = list(self.pending_evidence)
        self.pending_evidence.clear()
        return items


@dataclass
class Rule:
    rule_id: str
    name: str
    dimension: Dimension
    description: str
    requires: list[str] = field(default_factory=list)   # 需要的指标/科目
    packs: list[str] = field(default_factory=lambda: ["general"])
    # 明确不适用的行业包。行业适配的正确含义是“跳过不适用的规则”，
    # 而不是让特殊行业只跑行业规则包——财务质量、治理、监管类检查对所有行业都成立。
    exclude_packs: list[str] = field(default_factory=list)
    check: Callable[[RuleContext], tuple[RuleStatus, Severity, str, str]] = None  # type: ignore
    version: str = RULE_VERSION
    # 数据能力状态：enabled = 已具备可靠数据字段；unsupported_source = 数据源暂不支持。
    capability: str = Capability.ENABLED.value

    def evaluate(self, ctx: RuleContext) -> "RuleOutcome":
        if ctx.industry_pack in self.exclude_packs:
            return RuleOutcome(
                rule=self,
                status=RuleStatus.NOT_APPLICABLE,
                severity=Severity.UNKNOWN,
                finding=(
                    f"该检查项对 {ctx.industry_pack} 行业不适用"
                    + (f"，已由 {ctx.industry_pack} 行业规则包中的专门检查项替代"
                       if ctx.industry_pack == "realestate" else "")
                ),
                why="避免对特殊行业套用普通企业的财务结构标准",
                capability=self.capability,
            )
        if self.packs and ctx.industry_pack not in self.packs and "general" not in self.packs:
            return RuleOutcome(
                rule=self,
                status=RuleStatus.NOT_APPLICABLE,
                severity=Severity.UNKNOWN,
                finding=f"该检查项适用于 {'/'.join(self.packs)} 行业包，当前主体归类为 {ctx.industry_pack}",
                why="",
                capability=self.capability,
            )
        missing = self._missing(ctx)
        if missing:
            return RuleOutcome(
                rule=self,
                status=RuleStatus.INSUFFICIENT,
                severity=Severity.UNKNOWN,
                finding="缺少判断所需的数据:" + "、".join(missing),
                why="在已获取资料范围内无法判断，不输出无风险结论",
                to_verify=[f"补充 {m} 数据后重新检查" for m in missing],
                capability=self.capability,
            )
        try:
            status, severity, finding, why = self.check(ctx)
        except Exception as exc:  # 规则异常不得中断整次扫描
            return RuleOutcome(
                rule=self,
                status=RuleStatus.INSUFFICIENT,
                severity=Severity.UNKNOWN,
                finding=f"规则执行异常：{type(exc).__name__}",
                why="",
                to_verify=["检查该指标的数据质量后重试"],
                capability=self.capability,
            )
        return RuleOutcome(rule=self, status=status, severity=severity, finding=finding,
                           why=why, capability=self.capability)

    def _missing(self, ctx: RuleContext) -> list[str]:
        missing = []
        for req in self.requires:
            if req.startswith("metric:"):
                key = req.split(":", 1)[1]
                if not ctx.metrics.has(key):
                    missing.append(ctx.metrics.metrics.get(key).label if key in ctx.metrics.metrics else key)
            elif req.startswith("fact:"):
                key = req.split(":", 1)[1]
                if ctx.current_fact(key) is None:
                    missing.append(key)
        return missing


@dataclass
class RuleOutcome:
    rule: Rule
    status: RuleStatus
    severity: Severity
    finding: str
    why: str
    evidence_ids: list[str] = field(default_factory=list)
    mitigations: list[str] = field(default_factory=list)
    to_verify: list[str] = field(default_factory=list)
    strength: EvidenceStrength = EvidenceStrength.PARTIAL
    ai_interpreted: bool = False
    still_effective: Optional[bool] = None
    industry_pack: str = "general"
    capability: str = Capability.ENABLED.value

    def to_result(self) -> "object":
        from app.core.models import RuleResult

        return RuleResult(
            rule_id=self.rule.rule_id,
            name=self.rule.name,
            dimension=self.rule.dimension,
            status=self.status,
            severity=self.severity,
            strength=self.strength,
            finding=self.finding,
            why=self.why,
            evidence_ids=list(self.evidence_ids),
            mitigations=list(self.mitigations),
            to_verify=list(self.to_verify),
            industry_pack=self.industry_pack,
            rule_version=self.rule.version,
            ai_interpreted=self.ai_interpreted,
            still_effective=self.still_effective,
            capability=self.capability,
        )


class RuleRegistry:
    def __init__(self):
        self.rules: list[Rule] = []

    def register(self, rule: Rule) -> Rule:
        self.rules.append(rule)
        return rule

    def register_many(self, rules: list[Rule]) -> None:
        self.rules.extend(rules)

    def for_pack(self, pack: str) -> list[Rule]:
        return [r for r in self.rules if pack in r.packs or "general" in r.packs]

    def __len__(self) -> int:
        return len(self.rules)


def fnum(value: Optional[float], unit: str = "比率", digits: int = 2) -> str:
    if value is None:
        return "—"
    if unit == "比率":
        return f"{value * 100:.{digits}f}%"
    if unit == "倍":
        return f"{value:.{digits}f} 倍"
    if unit == "天":
        return f"{value:.0f} 天"
    return f"{value:,.{digits}f}"


def fmoney(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value / 1e8:,.2f} 亿元"
