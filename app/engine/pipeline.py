"""扫描流水线：方案 §4 的固定主流程。

1 身份识别 → 2 检索规划 → 3 资料获取 → 4 标准化 → 5 规则检查 →
6 专项阅读 → 7 核验 → 8 报告生成。

每个阶段写入检查点（数据库），进程重启后可继续或安全重试；
超时按当前进度生成带缺口的“部分完成”报告，不静默省略。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.core import db
from app.core.http_client import FetchError, HttpClient, summarize_records
from app.core.models import (
    Dimension,
    DisclosureDoc,
    Evidence,
    EVENT_CATEGORIES,
    Market,
    PeriodType,
    RiskEvent,
    RuleStatus,
    STAGE_ORDER,
    Severity,
    Stage,
    TaskStatus,
    now_iso,
)
from app.core.text import evidence_keywords
from app.data.cninfo import CninfoClient
from app.data.eastmoney import EastmoneyClient
from app.data.hkexnews import HkexnewsClient
from app.data.identity import IdentityResolver
from app.data.pdftext import build_evidence, parse_pdf
from app.engine.metrics import compute_metrics
from app.engine.normalize import FactSet
from app.engine.runner import (
    EngineOutput,
    RuleContext,
    ai_interpret,
    ai_verify,
    build_registry,
    run_rules,
)
from app.engine.rules.base import EvidenceStore, RULE_VERSION
from app.engine.rules.industry import classify_industry
from app.llm.adapter import LLMAdapter
from app.report.render import render_report

PRIORITY_TYPES = [
    "年报", "半年报", "审计", "财务更正", "监管处罚", "监管调查",
    "诉讼", "资产冻结", "监管问询", "上市地位", "审计机构", "股权质押",
    "关联交易", "高管变动", "盈利警告", "业绩预告", "季报", "担保",
]

TOPIC_KEYWORDS = {
    "现金流": ["经营活动产生的现金流量", "经营活动现金流量净额", "經營業務現金淨額"],
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
class ScanResult:
    task_id: str
    status: TaskStatus
    payload: dict[str, Any] = field(default_factory=dict)
    html_path: str = ""
    json_path: str = ""
    message: str = ""


class ScanPipeline:
    def __init__(self, task_id: str | None = None, deadline_seconds: int | None = None):
        self.task_id = task_id or uuid.uuid4().hex[:12]
        self.deadline = time.time() + (deadline_seconds if deadline_seconds is not None else settings.scan_timeout_seconds)
        self.gaps: list[str] = []
        self.stage_notes: dict[str, str] = {}
        self.notes: list[str] = []
        self.http = HttpClient()
        self.http.deadline = self.deadline
        self.em = EastmoneyClient(self.http)
        self.llm = LLMAdapter(task_id=self.task_id)
        self.llm.deadline = self.deadline

    # ------------------------------------------------------------ 工具

    def _checkpoint(self, stage: Stage) -> None:
        self.http.deadline = self.deadline
        self.llm.deadline = self.deadline
        db.update_task(
            self.task_id,
            status=TaskStatus.RUNNING.value,
            stage=stage.value,
            stage_index=STAGE_ORDER.index(stage),
        )

    def _time_left(self) -> float:
        return self.deadline - time.time()

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------ 主流程

    def run(self, query: str) -> ScanResult:
        db.create_task(self.task_id, query)
        started = time.time()
        db.update_task(
            self.task_id, status=TaskStatus.RUNNING.value,
            started_at=now_iso(), stage=Stage.IDENTIFY.value,
        )
        try:
            return self._run(query, started)
        except Exception as exc:  # 任何未捕获异常都记录为失败，不假装成功
            db.update_task(
                self.task_id, status=TaskStatus.FAILED.value,
                finished_at=now_iso(), error=f"{type(exc).__name__}: {exc}"[:500],
            )
            return ScanResult(self.task_id, TaskStatus.FAILED, message=str(exc)[:500])
        finally:
            self.close()

    def _run(self, query: str, started: float) -> ScanResult:
        # ---------- 1. 身份识别 ----------
        self._checkpoint(Stage.IDENTIFY)
        with IdentityResolver(self.em) as resolver:
            resolved = resolver.resolve(query)
        if not resolved.ok or not resolved.selected:
            db.update_task(
                self.task_id, status=TaskStatus.FAILED.value, finished_at=now_iso(),
                error=resolved.message,
            )
            return ScanResult(self.task_id, TaskStatus.FAILED, message=resolved.message)
        security = resolved.selected
        company = resolved.company
        self.gaps.extend(resolved.notes)
        db.update_task(self.task_id, secucode=security.secucode, market=security.market.value)
        db.save_fetch_logs(self.task_id, self.http.records)

        industry_pack = classify_industry(security.industry, security.org_name or security.name)
        if industry_pack != "general":
            self.notes.append(
                f"按行业“{security.industry or security.name}”启用 {industry_pack} 行业规则包，"
                f"普通企业规则中不适用项已自动跳过"
            )

        # ---------- 2. 检索规划 ----------
        self._checkpoint(Stage.PLAN)
        end = date.today()
        start = end - timedelta(days=30 * settings.announcement_months)
        plan = {
            "财务报告期": f"最近 {settings.fiscal_years_back} 个完整财年 + 最新已披露财报",
            "公告区间": f"{start.isoformat()} ~ {end.isoformat()}",
            "公告数量上限": settings.max_announcements,
            "原文下载上限": settings.max_pdf_downloads,
            "单文件解析页数上限": settings.max_pdf_pages,
            "行业规则包": industry_pack,
            "AI 解读": "启用" if self.llm.available else f"未启用（{self.llm.unavailable_reason}）",
        }
        self.stage_notes["检索规划"] = f"公告区间 {start} ~ {end}，行业规则包 {industry_pack}"

        # ---------- 3. 资料获取 ----------
        self._checkpoint(Stage.COLLECT)
        docs, announcement_meta = [], {"source": "", "total": 0, "range": f"{start} ~ {end}"}
        if self._time_left() > 0:
            try:
                docs, announcement_meta = self._collect_documents(security, start, end)
            except FetchError as exc:
                self.gaps.append(f"公告获取未完成：{exc}")
        else:
            self.gaps.append("任务期限已到，公告获取未执行")
        evidence_store, parsed_docs = self._build_evidence(security, docs)
        # 解析结果（页数、本地路径）落库，供进度页与重启恢复使用
        db.save_documents(self.task_id, docs)
        db.save_fetch_logs(self.task_id, self.http.records)

        # ---------- 4. 标准化 ----------
        self._checkpoint(Stage.NORMALIZE)
        raw_facts = []
        if self._time_left() > 0:
            try:
                raw_facts = (self.em.hk_statements(security.secucode) if security.market is Market.HK
                             else self.em.a_statements(security.secucode))
            except FetchError as exc:
                self.gaps.append(f"财务数据获取未完成：{exc}")
        else:
            self.gaps.append("任务期限已到，停止财务数据请求")
        if security.market is Market.HK:
            from app.engine.currency import verify_reporting_currencies
            self.gaps.extend(verify_reporting_currencies(raw_facts, docs, parsed_docs))
        facts = FactSet(raw_facts)
        self.gaps.extend(facts.errors())
        annual_periods = facts.periods(PeriodType.ANNUAL)
        if len(annual_periods) < settings.fiscal_years_back:
            self.gaps.append(f"计划覆盖 {settings.fiscal_years_back} 个完整财年，财务接口仅取得 {len(annual_periods)} 个年报期")
        db.save_facts(self.task_id, raw_facts)
        db.save_documents(self.task_id, docs)
        db.save_evidences(self.task_id, evidence_store.items.values())
        db.save_fetch_logs(self.task_id, self.http.records)

        if not facts.periods():
            self.gaps.append("未获取到任何财务报告期数据，全部财务类检查项将判定为数据不足")

        # ---------- 5. 规则检查 ----------
        self._checkpoint(Stage.RULE)
        metrics = compute_metrics(facts, market=security.market, industry=security.industry)
        for note in metrics.notes:
            (self.notes if note.startswith(("计算基准", "港股报表科目")) else self.gaps).append(note)
        ctx = RuleContext(
            security=security,
            market=security.market,
            facts=facts,
            metrics=metrics,
            docs=docs,
            parsed=parsed_docs,
            evidence=evidence_store,
            industry_pack=industry_pack,
            llm_enabled=self.llm.available,
            data_gaps=list(self.gaps),
        )
        registry = build_registry()
        output = run_rules(ctx, registry)

        # ---------- 6. 专项阅读 ----------
        self._checkpoint(Stage.READ)
        events, pending_clues = self._extract_events(docs, parsed_docs, evidence_store)
        ai_interpret(output, ctx, self.llm)

        # ---------- 7. 核验 ----------
        self._checkpoint(Stage.VERIFY)
        ai_verify(output, ctx, self.llm)
        verified_count = self._reverify_evidence(evidence_store, parsed_docs)
        from app.core.models import EvidenceStrength
        from app.engine.runner import refresh_coverage
        for outcome in output.outcomes:
            if outcome.strength is EvidenceStrength.CONFIRMED and not all(
                evidence_store.get(eid) and evidence_store.get(eid).verified for eid in outcome.evidence_ids
            ):
                outcome.strength = EvidenceStrength.WEAK
                outcome.to_verify.append("原文独立复核未通过，不能视为已确认结论")
        refresh_coverage(output)
        if self.llm.failures:
            self.gaps.extend(f"模型步骤未完整执行：{reason}" for reason in self.llm.failures)
        if output.insufficient():
            self.gaps.append(f"{len(output.insufficient())} 项检查缺少判断依据，详情见数据不足汇总")

        # ---------- 8. 报告生成 ----------
        self._checkpoint(Stage.REPORT)
        elapsed = time.time() - started
        timed_out = self._time_left() <= 0
        payload = self._build_payload(
            security=security,
            company=company.to_dict() if company else None,
            facts=facts,
            metrics=metrics,
            docs=docs,
            announcement_meta=announcement_meta,
            evidence_store=evidence_store,
            output=output,
            events=events,
            pending_clues=pending_clues,
            plan=plan,
            industry_pack=industry_pack,
            started=started,
            elapsed=elapsed,
            timed_out=timed_out,
            verification_count=verified_count,
            network=summarize_records(self.http.records),
        )
        html_path, json_path = render_report(payload)
        version = db.save_report(self.task_id, html_path, json_path, payload)
        db.save_rule_results(self.task_id, [o.to_result() for o in output.outcomes])
        db.save_risk_events(self.task_id, events)
        db.save_evidences(self.task_id, evidence_store.items.values())
        db.save_fetch_logs(self.task_id, self.http.records)

        status = TaskStatus.PARTIAL if (timed_out or self.gaps) else TaskStatus.SUCCEEDED
        if not any(
            o.status in (RuleStatus.RISK, RuleStatus.WATCH, RuleStatus.NORMAL)
            for o in output.outcomes
        ):
            status = TaskStatus.PARTIAL
        db.update_task(
            self.task_id,
            status=status.value,
            stage=Stage.REPORT.value,
            stage_index=len(STAGE_ORDER),
            finished_at=now_iso(),
            elapsed_ms=int(elapsed * 1000),
            rule_version=RULE_VERSION,
            data_snapshot=payload["data_scope"]["announcement_range"],
        )
        return ScanResult(
            self.task_id, status, payload,
            html_path=html_path, json_path=json_path,
            message="带有缺口的完整报告" if status is TaskStatus.PARTIAL else "完成",
        )

    # ------------------------------------------------------------ 阶段实现

    def _collect_documents(
        self, security, start: date, end: date
    ) -> tuple[list[DisclosureDoc], dict[str, Any]]:
        meta: dict[str, Any] = {"source": "", "gaps": []}
        docs: list[DisclosureDoc] = []
        if security.market is Market.HK:
            with HkexnewsClient(self.http) as hk:
                stock_id = hk.resolve_stock_id(security.code)
                out = hk.announcements(security.code, stock_id, start, end)
                docs, meta["gaps"] = out["docs"], out["gaps"]
                meta.update({"source": "hkexnews", "stock_id": stock_id,
                             "total": out["total"], "range": out["range"]})
                ordered = self._order_docs(docs)
                for doc in ordered[: settings.max_pdf_downloads]:
                    if self._time_left() < 60:
                        meta["gaps"].append("接近任务时限，停止下载剩余原文")
                        break
                    hk.download(doc)
        else:
            with CninfoClient(self.http) as cn:
                org = cn.resolve_org(security.code)
                if not org:
                    meta["gaps"].append(f"无法在巨潮定位 {security.code} 的主体标识，公告未获取")
                    self.gaps.extend(meta["gaps"])
                    return [], meta
                out = cn.announcements(security.code, org[0], start, end, column=org[1])
                docs, meta["gaps"] = out["docs"], out["gaps"]
                meta.update({"source": "cninfo", "org_id": org[0],
                             "total": out["total"], "range": out["range"]})
                ordered = self._order_docs(docs)
                for doc in ordered[: settings.max_pdf_downloads]:
                    if self._time_left() < 60:
                        meta["gaps"].append("接近任务时限，停止下载剩余原文")
                        break
                    cn.download(doc)
        if len(docs) > settings.max_pdf_downloads:
            meta["gaps"].append(f"{len(docs)} 份公告中仅下载最多 {settings.max_pdf_downloads} 份原文，其余仅检查标题")
        for doc in docs:
            if doc.parse_error:
                meta["gaps"].append(f"《{doc.title}》原文获取失败：{doc.parse_error}")
        self.gaps.extend(meta["gaps"])
        return docs, meta

    @staticmethod
    def _order_docs(docs: list[DisclosureDoc]) -> list[DisclosureDoc]:
        def rank(doc: DisclosureDoc) -> int:
            return PRIORITY_TYPES.index(doc.doc_type) if doc.doc_type in PRIORITY_TYPES else 99

        return sorted(sorted(docs, key=lambda d: d.publish_date, reverse=True), key=rank)

    def _build_evidence(
        self, security, docs: list[DisclosureDoc]
    ) -> tuple[EvidenceStore, dict[str, Any]]:
        store = EvidenceStore()
        parsed_docs: dict[str, Any] = {}
        base_keywords = evidence_keywords(
            security.org_name or security.name, security.code, security.market
        )
        for doc in docs:
            if not doc.local_path:
                continue
            if not settings.enable_pdf_parse:
                continue
            if self._time_left() <= 0:
                self.gaps.append("任务期限已到，停止剩余 PDF 解析")
                break
            parsed = parse_pdf(doc.local_path, timeout=min(60, self._time_left()))
            if parsed.truncated:
                self.gaps.append(f"《{doc.title}》仅解析 {len(parsed.pages)}/{parsed.page_count} 页，剩余正文未覆盖")
            parsed.doc_id = doc.doc_id
            parsed_docs[doc.doc_id] = parsed
            doc.parsed = not parsed.error
            doc.page_count = parsed.page_count
            doc.text_excerpt = parsed.pages[0][1][:200] if parsed.pages else ""
            if parsed.error:
                doc.parse_error = parsed.error
                self.gaps.append(f"《{doc.title}》解析失败：{parsed.error}")
                continue
            ev = build_evidence(doc, parsed, base_keywords)
            if ev:
                store.add(ev, doc_type=doc.doc_type)
            for topic, keywords in TOPIC_KEYWORDS.items():
                topic_ev = build_evidence(doc, parsed, keywords)
                if topic_ev:
                    store.add(topic_ev, topics=[topic], doc_type=doc.doc_type)
        return store, parsed_docs

    def _extract_events(
        self, docs: list[DisclosureDoc], parsed: dict[str, Any], store: Any = None
    ) -> tuple[list[RiskEvent], list[dict[str, Any]]]:
        """事件提取：确定性事件 + 模型候选事件。

        模型只能产生候选事件，必须携带 doc_id 与 evidence_quote，并在对应 ParsedDoc
        中定位完整引文、生成带文档/页码/指纹的 Evidence 且通过原文复核后，才能升级为
        正式事件；未通过者进入待核实线索，不参与正式时间线与风险计数。
        """
        formal: list[RiskEvent] = []
        clues: list[dict[str, Any]] = []
        risk_types = {
            "监管处罚", "监管调查", "诉讼", "资产冻结", "监管问询",
            "上市地位", "财务更正", "盈利警告", "审计机构", "股权质押",
        }
        existing: set[str] = set()
        # 1. 确定性事件：由程序按公告类型提取，直接作为正式事件。
        for doc in docs:
            if doc.doc_type not in risk_types:
                continue
            existing.add(doc.doc_id)
            quote = ""
            location = ""
            pdoc = parsed.get(doc.doc_id)
            if pdoc and pdoc.pages:
                location = f"第 {pdoc.pages[0][0]} 页"
                quote = pdoc.pages[0][1][:300]
            formal.append(
                RiskEvent(
                    event_id=f"evt:{doc.doc_id}",
                    title=doc.title,
                    occurred_date=doc.publish_date,
                    category=doc.doc_type,
                    summary=f"{doc.doc_type}类公告：{doc.title}" + (
                        f"；原文摘录：{quote[:120]}" if quote else ""
                    ),
                    source_doc_id=doc.doc_id,
                    resolved=None,
                )
            )
        # 2. 模型候选事件：须证据复核通过才升级为正式事件。
        if self.llm.available and parsed and self._time_left() > 0:
            context = [
                {
                    "doc_id": d.doc_id,
                    "title": d.title,
                    "date": d.publish_date,
                    "doc_type": d.doc_type,
                    "source": d.source,
                    "url": d.url,
                    "excerpt": (parsed[d.doc_id].pages[0][1][:600] if d.doc_id in parsed and parsed[d.doc_id].pages else ""),
                }
                for d in docs[:20]
            ]
            result = self.llm.extract_events(context)
            if result.ok:
                allowed = {item["doc_id"]: item for item in context}
                for item in result.data or []:
                    if not isinstance(item, dict):
                        continue
                    doc_id = str(item.get("doc_id") or "")
                    if not doc_id or doc_id not in allowed:
                        clues.append(
                            self._clue(item, "", "事件引用了不在本批输入中的公告 ID，已拒收")
                        )
                        continue
                    if doc_id in existing:
                        continue
                    event, clue = self._bind_candidate_event(item, allowed, parsed, store)
                    if event is not None:
                        formal.append(event)
                        existing.add(doc_id)
                    elif clue is not None:
                        clues.append(clue)
        formal.sort(key=lambda e: e.occurred_date, reverse=True)
        return formal, clues

    @staticmethod
    def _clue(item: dict[str, Any], doc_id: str, reason: str) -> dict[str, Any]:
        return {
            "doc_id": doc_id or str(item.get("doc_id") or ""),
            "title": str(item.get("title") or "")[:200],
            "category": str(item.get("category") or ""),
            "summary": str(item.get("summary") or "")[:500],
            "evidence_quote": str(item.get("evidence_quote") or "")[:500],
            "occurred_date": "",
            "reason": reason,
        }

    def _bind_candidate_event(
        self,
        item: dict[str, Any],
        allowed: dict[str, dict[str, Any]],
        parsed: dict[str, Any],
        store: Any,
    ) -> tuple[RiskEvent | None, dict[str, Any] | None]:
        """校验模型候选事件并绑定原文证据，返回 (正式事件, 待核实线索)。"""
        from app.data.pdftext import find_quote_page, verify_evidence

        doc_id = str(item.get("doc_id") or "")
        title = str(item.get("title") or "").strip()[:200]
        category = str(item.get("category") or "").strip()
        summary = str(item.get("summary") or "").strip()[:500]
        evidence_quote = str(item.get("evidence_quote") or "").strip()
        resolved = item.get("resolved")
        if not isinstance(resolved, bool) and resolved is not None:
            resolved = None
        resolution_note = str(item.get("resolution_note") or "").strip()[:300]
        occurred_date = str(allowed[doc_id].get("date") or "")

        def reject(reason: str) -> tuple[None, dict[str, Any]]:
            return None, self._clue(
                {
                    "doc_id": doc_id, "title": title, "category": category,
                    "summary": summary, "evidence_quote": evidence_quote,
                },
                doc_id,
                reason,
            )

        if category not in EVENT_CATEGORIES:
            return reject(f"事件类型「{category or '空'}」不在固定枚举中，已拒收")
        if not evidence_quote:
            return reject("模型未提供可引用的原文片段（evidence_quote 为空）")
        if len(evidence_quote) > 500:
            return reject("原文片段超过 500 字上限")
        if not occurred_date:
            return reject("程序未掌握该公告的日期，无法确定事件发生时间")

        pdoc = parsed.get(doc_id)
        if not pdoc or pdoc.error:
            return reject("对应公告未完成正文解析，无法复核引文")
        page = find_quote_page(pdoc, evidence_quote)
        if page is None:
            return reject("原文中未定位到完整引文（虚构引文或同前缀但尾部不符）")
        ev = Evidence(
            evidence_id=f"{doc_id}:p{page}:{Evidence.fingerprint_of(evidence_quote[:900])}",
            doc_id=doc_id,
            title=str(allowed[doc_id].get("title") or "")[:200],
            quote=evidence_quote[:900],
            location=f"第 {page} 页",
            url=str(allowed[doc_id].get("url") or ""),
            source=str(allowed[doc_id].get("source") or ""),
            publish_date=occurred_date,
            fingerprint=Evidence.fingerprint_of(evidence_quote[:900]),
        )
        verify_evidence(ev, pdoc)
        if not ev.verified:
            return reject("原文复核未通过（引文未出现在所标页码或指纹不一致）")
        if store is not None:
            store.add(ev, topics=["事件"], doc_type=str(allowed[doc_id].get("doc_type") or ""))

        event = RiskEvent(
            event_id=f"evt:ai:{doc_id}:{ev.fingerprint}",
            title=title or str(allowed[doc_id].get("title") or "")[:200],
            occurred_date=occurred_date,
            category=category,
            summary=summary,
            source_doc_id=doc_id,
            resolved=resolved,
            resolution_note=resolution_note,
            evidence_ids=[ev.evidence_id],
        )
        return event, None

    def _reverify_evidence(self, store: EvidenceStore, parsed: dict[str, Any]) -> int:
        """对每条证据做原文复核，未通过的标记为 unverified，报告必须可见。"""
        from app.data.pdftext import verify_evidence

        ok = 0
        for ev in store.items.values():
            doc_key = ev.doc_id
            pdoc = parsed.get(doc_key)
            if not pdoc:
                ev.verified = False
                ev.verify_note = "复核失败：未找到原文解析结果"
                continue
            verify_evidence(ev, pdoc)
            if ev.verified:
                ok += 1
        return ok

    # ------------------------------------------------------------ 报告数据

    def _build_payload(self, **kw: Any) -> dict[str, Any]:
        security = kw["security"]
        facts: FactSet = kw["facts"]
        metrics = kw["metrics"]
        output: EngineOutput = kw["output"]
        docs: list[DisclosureDoc] = kw["docs"]
        evidence_store: EvidenceStore = kw["evidence_store"]
        events = kw["events"]
        pending_clues = kw.get("pending_clues", [])

        highest = Severity.UNKNOWN
        for o in output.outcomes:
            if o.status is RuleStatus.RISK and o.severity is Severity.HIGH:
                highest = Severity.HIGH
                break
            if o.status is RuleStatus.RISK and highest is not Severity.HIGH:
                highest = Severity.MEDIUM

        by_dimension: dict[str, list[dict[str, Any]]] = {}
        for o in output.outcomes:
            by_dimension.setdefault(o.rule.dimension.value, []).append(
                {
                    "rule_id": o.rule.rule_id,
                    "name": o.rule.name,
                    "description": o.rule.description,
                    "status": o.status.value,
                    "severity": o.severity.value,
                    "strength": o.strength.value,
                    "finding": o.finding,
                    "why": o.why,
                    "evidence_ids": o.evidence_ids,
                    "mitigations": o.mitigations,
                    "to_verify": o.to_verify,
                    "still_effective": o.still_effective,
                    "ai_interpreted": o.ai_interpreted,
                    "industry_pack": o.industry_pack,
                }
            )

        # 趋势数据（用于 SVG 图）
        trends = self._build_trends(facts)

        payload: dict[str, Any] = {
            "report_version": "1.1",
            "rule_version": RULE_VERSION,
            "task_id": self.task_id,
            "generated_at": now_iso(),
            "scan": {
                "started_at": datetime.fromtimestamp(kw["started"]).isoformat(timespec="seconds"),
                "elapsed_seconds": round(kw["elapsed"], 1),
                "timed_out": kw["timed_out"],
                "status": "",
            },
            "security": security.to_dict(),
            "company": kw["company"],
            "industry_pack": kw["industry_pack"],
            "plan": kw["plan"],
            "data_scope": {
                "fiscal_periods": facts.periods()[:10],
                "latest_period": metrics.latest_period,
                "latest_period_label": (
                    f"{metrics.latest_period[:4]}年{metrics.latest_period_type.label}"
                    if metrics.latest_period and metrics.latest_period_type else ""
                ),
                "announcement_range": kw["announcement_meta"].get("range", ""),
                "announcement_total": kw["announcement_meta"].get("total", 0),
                "announcement_fetched": len(docs),
                "documents_downloaded": len([d for d in docs if d.local_path]),
                "documents_parsed": len([d for d in docs if d.parsed]),
                "evidence_count": len(evidence_store.items),
                "evidence_verified": kw["verification_count"],
                "currencies": facts.currencies(),
                "statements": facts.statements_present(),
                "source": kw["announcement_meta"].get("source", ""),
            },
            "summary": {
                "highest_severity": highest.value,
                "risk_count": len(output.risks()),
                "watch_count": len(output.watches()),
                "insufficient_count": len(output.insufficient()),
                "top_findings": [
                    {
                        "rule_id": o.rule.rule_id,
                        "name": o.rule.name,
                        "status": o.status.value,
                        "severity": o.severity.value,
                        "finding": o.finding,
                        "why": o.why,
                        "evidence_ids": o.evidence_ids,
                    }
                    for o in output.top_findings(5)
                ],
                "coverage": output.coverage.to_dict(),
            },
            "metrics": {
                "currency": metrics.currency,
                "latest_period": metrics.latest_period,
                "prior_period": metrics.prior_period,
                "items": metrics.as_dict(),
            },
            "trends": trends,
            "dimensions": [
                {"dimension": d, "results": by_dimension.get(d, [])}
                for d in [x.value for x in Dimension]
                if by_dimension.get(d)
            ],
            "timeline": [e.to_dict() for e in events],
            "pending_clues": pending_clues,
            "mitigations": self._collect_mitigations(output),
            "gaps": list(dict.fromkeys(self.gaps)),
            "notes": list(dict.fromkeys(self.notes)),
            "financial_facts": [f.to_dict() for f in facts.facts],
            "missing_data": self._collect_missing(output),
            "evidence": {k: v.to_dict() for k, v in evidence_store.items.items()},
            "documents": [d.to_dict() for d in docs],
            "ai": {
                "usage": self.llm.usage_summary(),
                "notes": output.ai_notes,
                "verification": output.verification,
            },
            "method": {
                "stages": [s.value for s in STAGE_ORDER],
                "network": kw["network"],
                "limitations": self._limitations(),
                "disclaimer": (
                    "本报告由程序计算指标、模型解释异常；原文证据的支持程度与核验结果分别标注。"
                    "报告不构成投资建议，不输出爆雷概率或安全评分。"
                    "检查结果仅限已获取资料，未覆盖部分见缺口说明。"
                ),
            },
        }
        payload["scan"]["status"] = "部分完成" if (kw["timed_out"] or self.gaps) else "完成"
        return payload

    def _build_trends(self, facts: FactSet) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for item in ("total_revenue", "operating_revenue", "revenue"):
            series = facts.series(item, limit=10)
            if series:
                out["revenue"] = [
                    {"period": p.period_end, "label": p.label, "value": p.value} for p in series
                ]
                break
        for item in ("net_profit_attributable", "net_profit"):
            series = facts.series(item, limit=10)
            if series:
                out["net_profit"] = [
                    {"period": p.period_end, "label": p.label, "value": p.value} for p in series
                ]
                break
        series = facts.series("ocf", limit=10)
        if series:
            out["ocf"] = [
                {"period": p.period_end, "label": p.label, "value": p.value} for p in series
            ]
        for item in ("total_assets",):
            series = facts.series(item, limit=10)
            if series:
                out["total_assets"] = [
                    {"period": p.period_end, "label": p.label, "value": p.value} for p in series
                ]
        for item in ("total_liabilities",):
            series = facts.series(item, limit=10)
            if series:
                out["total_liabilities"] = [
                    {"period": p.period_end, "label": p.label, "value": p.value} for p in series
                ]
        return out

    @staticmethod
    def _collect_mitigations(output: EngineOutput) -> list[dict[str, str]]:
        out = []
        for o in output.outcomes:
            for m in o.mitigations:
                out.append({"rule_id": o.rule.rule_id, "name": o.rule.name, "text": m})
        return out

    @staticmethod
    def _collect_missing(output: EngineOutput) -> list[dict[str, str]]:
        out = []
        for o in output.outcomes:
            if o.status is RuleStatus.INSUFFICIENT:
                out.append(
                    {"rule_id": o.rule.rule_id, "name": o.rule.name, "reason": o.finding}
                )
        return out

    def _limitations(self) -> list[str]:
        items = [
            "财务数据来自东方财富数据中心公开接口，非交易所官方授权数据，字段口径以接口返回为准；",
            "公告原文来自巨潮资讯网（A 股）与港交所披露易（港股）公开页面；",
            f"单次扫描最多下载 {settings.max_pdf_downloads} 份原文，每份最多解析 {settings.max_pdf_pages} 页，"
            "超出部分不会出现在证据中；",
            "港股报表按原会计准则与币种呈现，未做准则转换与汇率折算；",
            "行业监管阈值（资本充足率、偿付能力、净资本等）一旦数据源未提供即判定为数据不足，不做估算。",
        ]
        if not self.llm.available:
            items.append(f"本次未启用模型解读：{self.llm.unavailable_reason}；报告中不含 AI 解释性结论。")
        return items
