"""规则引擎执行器：规则检查 → 证据绑定 → AI 解读 → 独立核验 → 覆盖统计。

方案 §4 的 5—7 步在此落地。AI 只解释已经算出的异常，不生成指标；
核验步骤独立于解读步骤，模型之间的相互认可不作为事实成立依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.models import (
    Evidence,
    EvidenceStrength,
    RuleStatus,
    Severity,
)
from app.engine.rules.base import (
    RuleContext,
    RuleOutcome,
    RuleRegistry,
)
from app.engine.rules.general import _dim_doc_keywords  # 维度 → 文档类型/关键词
from app.llm.adapter import LLMAdapter

# 规则 → 证据主题
RULE_EVIDENCE_TOPICS: dict[str, list[str]] = {
    "FQ01": ["现金流"], "FQ02": ["现金流"], "FQ03": ["应收"], "FQ04": ["应收"],
    "FQ05": ["存货"], "FQ11": ["现金流"],
    "SV01": ["借款"], "SV02": ["借款"], "SV05": ["借款"], "SV06": ["现金流"],
    "SV08": ["担保"], "GV03": ["质押冻结"], "GV04": ["关联交易"],
    "RG01": ["违规"], "RG02": ["诉讼"], "RG03": ["违规"], "RG05": ["质押冻结"],
    "OP02": ["审计意见"], "OP03": ["现金流"], "OP04": ["减值"], "OP05": ["现金流"],
    "BK01": ["借款"], "BR02": ["借款"], "RE01": ["借款"], "RE02": ["借款"],
    "RE03": ["存货"], "RE04": ["存货"],
}

# 主题 → 原文检索关键词
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "现金流": ["经营活动产生的现金流量", "经营活动现金流量净额", "經營業務現金淨額", "經營活動"],
    "应收": ["应收账款", "應收帳款", "應收賬款"],
    "存货": ["存货", "存貨"],
    "借款": ["短期借款", "短期貸款", "长期借款", "長期貸款"],
    "担保": ["担保", "擔保"],
    "质押冻结": ["质押", "質押", "冻结", "凍結"],
    "关联交易": ["关联交易", "關連交易", "關聯交易"],
    "违规": ["处罚", "處罰", "立案", "调查", "調查", "违规", "違規", "问询", "問詢"],
    "诉讼": ["诉讼", "訴訟", "仲裁"],
    "审计意见": ["审计意见", "審計意見", "保留意见", "保留意見",
                 "无法表示意见", "無法表示意見", "持续经营", "持續經營"],
    "减值": ["减值", "減值", "商誉", "商譽"],
}


@dataclass
class Coverage:
    applicable: int = 0
    evaluated: int = 0
    insufficient: int = 0
    not_applicable: int = 0
    by_dimension: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "evaluated": self.evaluated,
            "insufficient": self.insufficient,
            "not_applicable": self.not_applicable,
            "completeness": (
                round(self.evaluated / self.applicable, 4) if self.applicable else 0.0
            ),
            "by_dimension": self.by_dimension,
        }


@dataclass
class EngineOutput:
    outcomes: list[RuleOutcome] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    ai_notes: list[str] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)

    def risks(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.status is RuleStatus.RISK]

    def watches(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.status is RuleStatus.WATCH]

    def insufficient(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.status is RuleStatus.INSUFFICIENT]

    def top_findings(self, limit: int = 5) -> list[RuleOutcome]:
        order = {RuleStatus.RISK: 0, RuleStatus.WATCH: 1}
        sev = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2, Severity.UNKNOWN: 3}
        candidates = [o for o in self.outcomes if o.status in order]
        candidates.sort(key=lambda o: (order[o.status], sev[o.severity], o.rule.rule_id))
        return candidates[:limit]


def build_registry() -> RuleRegistry:
    from app.engine.rules.general import build_general_rules
    from app.engine.rules.industry import (
        build_bank_rules,
        build_broker_rules,
        build_insurance_rules,
        build_realestate_rules,
    )

    registry = RuleRegistry()
    registry.register_many(build_general_rules())
    registry.register_many(build_bank_rules())
    registry.register_many(build_insurance_rules())
    registry.register_many(build_broker_rules())
    registry.register_many(build_realestate_rules())
    return registry


def attach_evidence(outcome: RuleOutcome, ctx: RuleContext) -> None:
    """绑定证据。

    优先级：规则精确定位的原文片段 > 主题命中 > 标题命中 > 文档类型命中。
    只有当更精确的一层没有命中时，才退到下一层，避免把无关文件当成证据。
    """
    if outcome.status in (RuleStatus.NORMAL, RuleStatus.NOT_APPLICABLE):
        return
    ids: list[str] = []

    for doc, quote, location, topics in ctx.take_pending_evidence():
        ev = Evidence(
            evidence_id=f"{doc.doc_id}:loc{Evidence.fingerprint_of(quote)}",
            doc_id=doc.doc_id,
            title=doc.title,
            quote=quote[:900],
            location=location,
            url=doc.url,
            source=doc.source,
            publish_date=doc.publish_date,
            fingerprint=Evidence.fingerprint_of(quote),
            verified=True,
            verify_note="规则检查时定位的原文片段",
        )
        ids.append(ctx.evidence.add(ev, topics=topics, doc_type=doc.doc_type))

    for topic in RULE_EVIDENCE_TOPICS.get(outcome.rule.rule_id, []):
        ids.extend(ctx.evidence.by_topic(topic, limit=2))

    doc_types, keywords = _dim_doc_keywords(outcome.rule.dimension)
    if not ids and keywords:
        ids.extend(ctx.evidence.by_titles(keywords, limit=2))
    if not ids and doc_types:
        ids.extend(ctx.evidence.by_doc_types(doc_types, limit=2))

    seen: set[str] = set()
    outcome.evidence_ids = [i for i in ids if not (i in seen or seen.add(i))][:6]
    if outcome.evidence_ids and outcome.status is RuleStatus.RISK:
        outcome.strength = EvidenceStrength.CONFIRMED
    elif outcome.evidence_ids:
        outcome.strength = EvidenceStrength.PARTIAL
    else:
        outcome.strength = EvidenceStrength.WEAK


def run_rules(ctx: RuleContext, registry: RuleRegistry) -> EngineOutput:
    output = EngineOutput()
    coverage = Coverage()
    for rule in registry.rules:
        outcome = rule.evaluate(ctx)
        outcome.industry_pack = ctx.industry_pack
        attach_evidence(outcome, ctx)
        output.outcomes.append(outcome)

        dim = rule.dimension.value
        bucket = coverage.by_dimension.setdefault(
            dim, {"总数": 0, "已判断": 0, "数据不足": 0, "不适用": 0}
        )
        bucket["总数"] += 1
        if outcome.status is RuleStatus.NOT_APPLICABLE:
            coverage.not_applicable += 1
            bucket["不适用"] += 1
        else:
            coverage.applicable += 1
            if outcome.status is RuleStatus.INSUFFICIENT:
                coverage.insufficient += 1
                bucket["数据不足"] += 1
            else:
                coverage.evaluated += 1
                bucket["已判断"] += 1
    output.coverage = coverage
    return output


# --------------------------------------------------------------- AI 解读


def ai_interpret(output: EngineOutput, ctx: RuleContext, llm: LLMAdapter) -> None:
    """让模型解释程序算出的异常，并识别缓解因素与后续进展。

    未配置模型时，本步骤整体跳过，并在报告中标注“未启用”，不生成任何替代结论。
    """
    if not llm.available:
        output.ai_notes.append(f"AI 解读未执行：{llm.unavailable_reason}")
        return

    targets = [
        o for o in output.outcomes
        if o.status in (RuleStatus.RISK, RuleStatus.WATCH)
    ]
    if not targets:
        return

    items = []
    for o in targets:
        evidence_texts = []
        for eid in o.evidence_ids[:3]:
            ev = ctx.evidence.get(eid)
            if ev:
                evidence_texts.append(
                    {"doc": ev.title, "location": ev.location, "quote": ev.quote[:400]}
                )
        items.append(
            {
                "rule_id": o.rule.rule_id,
                "name": o.rule.name,
                "status": o.status.value,
                "finding": o.finding,
                "why": o.why,
                "company": ctx.security.org_name or ctx.security.name,
                "period": ctx.metrics.latest_period,
                "evidence": evidence_texts,
            }
        )

    result = llm.interpret_anomalies({"items": items})
    if not result.ok:
        output.ai_notes.append(f"AI 解读未生效：{result.error or result.skipped_reason}")
        return

    by_id = {str(i.get("rule_id")): i for i in (result.data or []) if isinstance(i, dict)}
    matched = 0
    for o in targets:
        payload = by_id.get(o.rule.rule_id)
        if not payload:
            continue
        matched += 1
        o.ai_interpreted = True
        explanation = str(payload.get("explanation") or "").strip()
        if explanation:
            o.why = explanation
        if payload.get("mitigations"):
            o.mitigations = [str(m) for m in payload["mitigations"]][:4]
        if payload.get("to_verify"):
            o.to_verify = [str(v) for v in payload["to_verify"]][:4]
        still = payload.get("still_effective")
        if isinstance(still, bool):
            o.still_effective = still
    output.ai_notes.append(
        f"AI 解读完成：{matched}/{len(targets)} 项异常获得模型解释（模型 {llm.config.model}）"
    )


def ai_verify(output: EngineOutput, ctx: RuleContext, llm: LLMAdapter) -> None:
    """独立核验：检查结论与证据在主体、时间、语义上是否对应。"""
    if not llm.available or not llm.config.verify_enabled:
        output.ai_notes.append(
            f"独立核验未执行：{'' if llm.available else llm.unavailable_reason}"
            f"{'；已在配置中关闭核验' if llm.available and not llm.config.verify_enabled else ''}"
        )
        return

    targets = [o for o in output.outcomes if o.status is RuleStatus.RISK and o.evidence_ids]
    if not targets:
        return

    payload = []
    for o in targets:
        for eid in o.evidence_ids[:2]:
            ev = ctx.evidence.get(eid)
            if not ev:
                continue
            payload.append(
                {
                    "rule_id": o.rule.rule_id,
                    "conclusion": o.finding,
                    "company": ctx.security.org_name or ctx.security.name,
                    "evidence_title": ev.title,
                    "evidence_date": ev.publish_date,
                    "evidence_location": ev.location,
                    "evidence_quote": ev.quote[:500],
                }
            )
    if not payload:
        return

    result = llm.verify({"items": payload})
    if not result.ok:
        output.ai_notes.append(f"独立核验未生效：{result.error or result.skipped_reason}")
        return

    verdicts = [v for v in (result.data or []) if isinstance(v, dict)]
    downgraded = 0
    for v in verdicts:
        rid = str(v.get("rule_id"))
        outcome = next((o for o in targets if o.rule.rule_id == rid), None)
        if not outcome:
            continue
        output.verification.append(
            {
                "rule_id": rid,
                "verdict": str(v.get("verdict") or ""),
                "reason": str(v.get("reason") or "")[:300],
                "suggested_status": str(v.get("suggested_status") or ""),
            }
        )
        suggestion = str(v.get("suggested_status") or "")
        if "降级为需要关注" in suggestion and outcome.status is RuleStatus.RISK:
            outcome.status = RuleStatus.WATCH
            outcome.strength = EvidenceStrength.PARTIAL
            downgraded += 1
        elif "降级为数据不足" in suggestion:
            outcome.status = RuleStatus.INSUFFICIENT
            outcome.strength = EvidenceStrength.WEAK
            downgraded += 1
    output.ai_notes.append(
        f"独立核验完成：核验 {len(verdicts)} 条证据，{downgraded} 项结论被下调"
    )
