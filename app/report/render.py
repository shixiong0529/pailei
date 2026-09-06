"""报告渲染：结构化结果 → 独立 HTML + JSON。

安全约束（方案 §8）：
- 使用受控模板，模型不直接生成页面代码；
- 所有动态文字统一转义；
- 来源链接只允许 http/https；
- 样式与图表内联，离线可阅读、可打印。
"""

from __future__ import annotations

import json
import copy
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape, StrictUndefined

from app.config import settings
from app.core.models import now_iso
from app.report import charts
from app.engine.rules.base import RULE_VERSION

TEMPLATE_DIR = Path(__file__).parent / "templates"

STATUS_CLASS = {
    "发现风险": "risk",
    "需要关注": "watch",
    "已覆盖资料中未发现明显异常": "normal",
    "数据不足，无法判断": "insufficient",
    "不适用": "na",
}

SEVERITY_CLASS = {"高": "sev-high", "中": "sev-mid", "低": "sev-low", "未定": "sev-unknown"}

# 风险信号评分：发现风险 高-12 / 中-8 / 低-4；需要关注 中-2 / 低-1
RISK_DEDUCTION = {"高": 12, "中": 8, "低": 4}
WATCH_DEDUCTION = {"中": 2, "低": 1}
# 未知严重度按中档扣，避免真实风险零扣分
RISK_DEFAULT_DEDUCTION = 8
WATCH_DEFAULT_DEDUCTION = 1
GRADE_BANDS = [
    (95, "A", "未发现明显风险信号"),
    (80, "B", "轻度关注"),
    (60, "C", "需要留意"),
    (35, "D", "风险信号密集"),
    (0, "E", "高风险"),
]


def risk_signal_score(dimensions: list[dict[str, Any]]) -> dict[str, Any]:
    """从维度检查结果加权计算风险信号评分。

    基础分 100，按检查项结论与严重度扣分，下限 0。
    "数据不足，无法判断" 与 "不适用" 不扣分。
    """
    risk_counts = {"高": 0, "中": 0, "低": 0, "未定": 0}
    watch_counts = {"中": 0, "低": 0, "未定": 0}
    risk_deduction = 0
    watch_deduction = 0
    for dim in dimensions or []:
        for r in dim.get("results") or []:
            status = r.get("status")
            sev = r.get("severity")
            if status == "发现风险":
                points = RISK_DEDUCTION.get(sev, RISK_DEFAULT_DEDUCTION)
                risk_counts[sev if sev in RISK_DEDUCTION else "未定"] += 1
                risk_deduction += points
            elif status == "需要关注":
                points = WATCH_DEDUCTION.get(sev, WATCH_DEFAULT_DEDUCTION)
                watch_counts[sev if sev in WATCH_DEDUCTION else "未定"] += 1
                watch_deduction += points

    score = max(0, 100 - risk_deduction - watch_deduction)
    for threshold, grade, label in GRADE_BANDS:
        if score >= threshold:
            break

    def _comp(counts: dict[str, int], per: dict[str, int], default: int, order: list[str]) -> str:
        parts = [
            f"{sev} {counts[sev]}×{per.get(sev, default)}"
            for sev in order
            if counts.get(sev)
        ]
        return " ＋ ".join(parts) if parts else "无"

    risk_comp = _comp(risk_counts, RISK_DEDUCTION, RISK_DEFAULT_DEDUCTION, ["高", "中", "低", "未定"])
    watch_comp = _comp(watch_counts, WATCH_DEDUCTION, WATCH_DEFAULT_DEDUCTION, ["中", "低", "未定"])
    return {
        "score": score,
        "grade": grade,
        "label": label,
        "risk_deduction": risk_deduction,
        "watch_deduction": watch_deduction,
        "risk_counts": risk_counts,
        "watch_counts": watch_counts,
        "risk_comp": risk_comp,
        "watch_comp": watch_comp,
    }

TREND_SPECS = [
    ("revenue", "营业收入", "#2563eb"),
    ("net_profit", "净利润", "#7c3aed"),
    ("ocf", "经营活动现金流净额", "#0891b2"),
    ("total_assets", "总资产", "#475569"),
    ("total_liabilities", "总负债", "#94a3b8"),
]

_dimension_desc = {
    "财务质量": "利润与现金流是否匹配、应收与存货是否异常、是否依赖非经常性损益",
    "偿债能力": "现金对短债的覆盖、杠杆水平、付息能力与表外义务",
    "公司治理": "审计机构与关键人员稳定性、股权质押、关联交易与股东行为",
    "监管法律": "立案调查、行政处罚、诉讼仲裁、资产冻结与上市地位",
    "经营行业": "经营预警、审计意见、资本开支、减值压力与持续经营",
}


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def safe_url(url: str) -> str:
    """只允许 http/https 链接进入报告。"""
    if not url:
        return ""
    parsed = urlparse(str(url))
    if parsed.scheme.lower() in ("http", "https"):
        return str(url)
    return ""


def _reverse(series: list[dict]) -> list[dict]:
    return sorted(series, key=lambda p: p.get("period", ""))


def build_context(payload: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(payload)
    # 旧版本未记录的可选展示元数据，在输入边界显式提供兼容默认；模板仍严格检查变量名。
    defaults = {
        "security": {"exchange": "", "industry": "", "currency": ""},
        "scan": {"started_at": "", "status": "", "timed_out": False},
        "data_scope": {"announcement_range": "", "announcement_fetched": 0, "documents_downloaded": 0,
                       "evidence_count": 0, "evidence_verified": 0, "latest_period_label": "", "latest_period": ""},
        "summary": {"risk_count": 0, "watch_count": 0, "insufficient_count": 0, "highest_severity": "未定", "top_findings": []},
        "method": {"disclaimer": "", "limitations": []},
        "ai": {"notes": [], "verification": []},
        "metrics": {"currency": "未核实"},
    }
    for key, fallback in defaults.items():
        payload[key] = {**fallback, **(payload.get(key) or {})}
    payload["ai"]["usage"] = {"available": False, "model": "", "calls": 0, "cache_hits": 0, "spent_cny": 0,
                              "reason": "", "failures": [], **(payload["ai"].get("usage") or {})}
    for dim in payload.get("dimensions") or []:
        for result in dim.get("results") or []:
            for key, value in {"evidence_ids": [], "mitigations": [], "to_verify": [], "still_effective": None,
                               "ai_interpreted": False, "strength": "线索待核实", "why": ""}.items():
                result.setdefault(key, value)
    # V1.2 生命周期字段：旧报告或测试载荷的时间线条目可能缺少这些键，渲染前补齐默认。
    for ev in payload.get("timeline") or []:
        for key, value in {"lifecycle_stage": "", "summary": "", "resolved": None,
                           "resolution_note": "", "resolution_basis": "", "resolution_date": "",
                           "evidence_ids": []}.items():
            ev.setdefault(key, value)
    trends = payload.get("trends") or {}
    chart_blocks = []
    for key, title, color in TREND_SPECS:
        series = trends.get(key)
        if not series:
            continue
        chart_blocks.append(
            {
                "title": title,
                "svg": charts.trend_chart(_reverse(series), title, color=color),
                "latest": _reverse(series)[-1] if series else None,
                "count": len(series),
            }
        )

    summary = payload.get("summary") or {}
    coverage = summary.get("coverage") or {}
    dimensions = payload.get("dimensions") or []
    risk_score = risk_signal_score(dimensions)
    evaluated = sum(r.get("status") in {"发现风险", "需要关注", "已覆盖资料中未发现明显异常"}
                    for dim in dimensions for r in dim.get("results") or [])
    if coverage.get("evaluated", evaluated) == 0 or evaluated == 0:
        risk_score.update(score="—", grade="—", label="资料不足，暂不形成评级")
    elif summary.get("insufficient_count") or payload.get("gaps"):
        if risk_score["grade"] == "A":
            risk_score["label"] = "已覆盖项目风险信号较少，仍有资料缺口"
    coverage = {"evaluated": evaluated, "applicable": evaluated, "insufficient": 0, **coverage}

    metrics = payload.get("metrics") or {}
    metric_items = list((metrics.get("items") or {}).values())
    metric_items = [m for m in metric_items if m.get("available")]

    # 证据索引
    evidence_map = payload.get("evidence") or {}
    documents = payload.get("documents") or []

    return {
        "payload": payload,
        "current_rule_version": RULE_VERSION,
        "security": payload.get("security") or {},
        "company": payload.get("company") or {},
        "scan": payload.get("scan") or {},
        "data_scope": payload.get("data_scope") or {},
        "summary": summary,
        "coverage": coverage,
        "metrics": metrics,
        "industry_pack": payload.get("industry_pack") or "general",
        "chart_blocks": chart_blocks,
        "risk_score": risk_score,
        "coverage_svg": charts.coverage_donut(
            coverage.get("applicable", 0),
            coverage.get("evaluated", 0),
            coverage.get("insufficient", 0),
        ),
        "metric_items": metric_items,
        "dimensions": dimensions,
        "dimension_desc": _dimension_desc,
        "timeline": payload.get("timeline") or [],
        "lifecycles": payload.get("lifecycles") or [],
        "pending_clues": payload.get("pending_clues") or [],
        "mitigations": payload.get("mitigations") or [],
        "gaps": payload.get("gaps") or [],
        "notes": payload.get("notes") or [],
        "missing_data": payload.get("missing_data") or [],
        "evidence_map": evidence_map,
        "documents": documents,
        "ai": payload.get("ai") or {},
        "method": payload.get("method") or {},
        "plan": payload.get("plan") or {},
        "status_class": STATUS_CLASS,
        "severity_class": SEVERITY_CLASS,
        "safe_url": safe_url,
        "rendered_at": payload.get("generated_at") or payload.get("scan", {}).get("started_at") or "未记录",
    }


def render_report(payload: dict[str, Any]) -> tuple[str, str]:
    """渲染并落盘，返回 (html_path, json_path)。"""
    context = build_context(payload)
    template = _env().get_template("report.html.j2")
    html = template.render(**context)

    reports_dir = settings.reports_dir
    if not reports_dir.exists():
        reports_dir.mkdir(parents=True)
    task_id = payload.get("task_id", "unknown")
    html_path = reports_dir / f"{task_id}.html"
    json_path = reports_dir / f"{task_id}.json"
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return str(html_path), str(json_path)


def render_inline(payload: dict[str, Any]) -> str:
    """在线预览使用同一模板，保证在线与下载内容一致。"""
    return _env().get_template("report.html.j2").render(**build_context(payload))
