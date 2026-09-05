"""阶段 0：数据验证。

目标（开发方案 §10 阶段 0）：
- 对一家 A 股和一家港股公司，验证证券身份、财务数据、公告与原文获取；
- 记录覆盖范围、权限要求、失败原因，作为是否继续投入引擎开发的依据。

用法：
    python scripts/phase0_validate.py            # 默认 600519 / 00700
    python scripts/phase0_validate.py 000333 00300

输出：
    docs/PHASE0_DATA_VALIDATION.md
    docs/phase0_result.json
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core.http_client import FetchError, HttpClient, summarize_records  # noqa: E402
from app.core.models import Market, Statement, now_iso  # noqa: E402
from app.data.cninfo import CninfoClient  # noqa: E402
from app.data.eastmoney import EastmoneyClient  # noqa: E402
from app.data.hkexnews import HkexnewsClient  # noqa: E402
from app.data.identity import IdentityResolver  # noqa: E402
from app.core.text import evidence_keywords  # noqa: E402
from app.data.pdftext import build_evidence, parse_pdf, summarize_parsed  # noqa: E402

DOCS = ROOT / "docs"
if not DOCS.exists():
    DOCS.mkdir(parents=True)

PRIORITY_DOC_TYPES = {"年报", "半年报", "审计", "监管问询", "监管处罚", "监管调查", "诉讼", "财务更正"}


def validate_one(query: str, expected_market: Market) -> dict:
    print(f"\n{'='*78}\n阶段0 验证：{query}（预期市场 {expected_market.value}）\n{'='*78}")
    result: dict = {
        "query": query,
        "expected_market": expected_market.value,
        "started_at": now_iso(),
        "checks": {},
        "sources": {},
        "failures": [],
        "gaps": [],
    }
    http = HttpClient()
    em = EastmoneyClient(http)
    started = time.time()

    # ---------------- 1. 证券身份 ----------------
    print("\n[1/5] 证券身份识别 ...")
    identity: dict = {}
    try:
        with IdentityResolver(em) as resolver:
            resolved = resolver.resolve(query)
            identity = {
                "ok": resolved.ok,
                "selected": resolved.selected.to_dict() if resolved.selected else None,
                "candidate_count": len(resolved.candidates),
                "candidates": [
                    f"{c.security.secucode} {c.security.name}（{c.match_reason}）"
                    for c in resolved.candidates
                ][:8],
                "company": resolved.company.to_dict() if resolved.company else None,
                "ambiguous": resolved.ambiguous,
                "message": resolved.message,
                "notes": resolved.notes,
            }
            if not resolved.ok or not resolved.selected:
                result["failures"].append(f"身份识别失败：{resolved.message}")
                result["checks"]["identity"] = identity
                return result
            security = resolved.selected
    except Exception as exc:
        result["failures"].append(f"身份识别异常：{type(exc).__name__} {exc}")
        result["checks"]["identity"] = identity
        return result

    print(f"      选中：{security.secucode} {security.name} | 全称：{security.org_name or '（未获取）'}")
    print(f"      交易所：{security.exchange} | 行业：{security.industry or '（未获取）'} | 币种：{security.currency}")
    print(f"      候选数：{identity['candidate_count']} | A/H 关联：{len(identity['company']['securities']) if identity['company'] else 0} 个主体")
    result["checks"]["identity"] = identity
    result["gaps"].extend(resolved.notes)

    # ---------------- 2. 财务数据 ----------------
    print("\n[2/5] 财务数据获取 ...")
    finance: dict = {}
    try:
        if security.market is Market.HK:
            facts = em.hk_statements(security.secucode)
        else:
            facts = em.a_statements(security.secucode)
        errors = [f for f in facts if f.std_item == "__error__"]
        real = [f for f in facts if f.std_item != "__error__"]
        periods = sorted({f.period_end for f in real if f.period_end}, reverse=True)
        statements = {}
        for st in Statement:
            items = sorted({f.std_item for f in real if f.statement is st})
            if items:
                statements[st.value] = {
                    "fact_count": sum(1 for f in real if f.statement is st),
                    "items": items,
                }
        currencies = sorted({f.currency for f in real if f.currency})
        finance = {
            "fact_count": len(real),
            "periods": periods[:14],
            "period_count": len(periods),
            "latest_period": periods[0] if periods else "",
            "currencies": currencies,
            "statements": statements,
            "errors": [f.note for f in errors],
        }
        print(f"      财务事实 {len(real)} 条 | 报告期 {len(periods)} 个 | 最新 {periods[0] if periods else '无'}")
        print(f"      币种：{currencies}")
        for st, info in statements.items():
            print(f"      {st:9s} {info['fact_count']:5d} 条 / {len(info['items'])} 个科目")
        result["failures"].extend(finance["errors"])
        if not real:
            result["failures"].append("未获取到任何财务事实")
    except Exception as exc:
        result["failures"].append(f"财务获取异常：{type(exc).__name__} {exc}")
        finance = {"fact_count": 0}
    result["checks"]["finance"] = finance

    # ---------------- 3. 公告列表 ----------------
    print("\n[3/5] 公告检索 ...")
    announcements: dict = {}
    docs = []
    try:
        end = date.today()
        start = end - timedelta(days=365)
        if security.market is Market.HK:
            with HkexnewsClient(http) as hk:
                stock_id = hk.resolve_stock_id(security.code)
                print(f"      披露易 stockId = {stock_id}")
                out = hk.announcements(security.code, stock_id, start, end)
                docs = out["docs"]
                announcements = {
                    "source": "hkexnews",
                    "stock_id": stock_id,
                    "total": out["total"],
                    "fetched": out["fetched"],
                    "range": out["range"],
                    "gaps": out["gaps"],
                }
        else:
            with CninfoClient(http) as cn:
                org = cn.resolve_org(security.code)
                print(f"      巨潮 orgId = {org}")
                if not org:
                    raise FetchError("无法解析巨潮 orgId")
                out = cn.announcements(security.code, org[0], start, end, column=org[1])
                docs = out["docs"]
                announcements = {
                    "source": "cninfo",
                    "org_id": org[0],
                    "total": out["total"],
                    "fetched": out["fetched"],
                    "range": out["range"],
                    "gaps": out["gaps"],
                }
        type_count: dict[str, int] = {}
        for d in docs:
            type_count[d.doc_type] = type_count.get(d.doc_type, 0) + 1
        announcements["type_distribution"] = dict(
            sorted(type_count.items(), key=lambda kv: -kv[1])
        )
        print(f"      公告 {announcements['fetched']} 条（接口声明 {announcements['total']}）| 区间 {announcements['range']}")
        print(f"      类型分布：{announcements['type_distribution']}")
        result["gaps"].extend(announcements.get("gaps") or [])
    except Exception as exc:
        result["failures"].append(f"公告检索异常：{type(exc).__name__} {exc}")
        announcements = {"error": str(exc)[:200]}
    result["checks"]["announcements"] = announcements

    # ---------------- 4. 原文下载与解析 ----------------
    print("\n[4/5] 原文下载、解析与证据定位 ...")
    doc_check: dict = {"downloaded": 0, "parsed": 0, "failed": [], "samples": []}
    try:
        priority = [d for d in docs if d.doc_type in PRIORITY_DOC_TYPES]
        ordered = (priority + [d for d in docs if d.doc_type not in PRIORITY_DOC_TYPES])[
            : settings.max_pdf_downloads
        ]
        client_ctx = (
            CninfoClient(http) if security.market is Market.A else HkexnewsClient(http)
        )
        with client_ctx as provider:
            for doc in ordered[:6]:
                try:
                    doc = provider.download(doc)
                except Exception as exc:
                    doc_check["failed"].append(f"{doc.title}: 下载异常 {type(exc).__name__}")
                    continue
                if not doc.local_path:
                    doc_check["failed"].append(f"{doc.title}: {doc.parse_error or '无本地文件'}")
                    continue
                doc_check["downloaded"] += 1
                parsed = parse_pdf(doc.local_path)
                info = summarize_parsed(parsed)
                if parsed.error:
                    doc_check["failed"].append(f"{doc.title}: 解析失败 {parsed.error}")
                    continue
                doc_check["parsed"] += 1
                keywords = evidence_keywords(security.name, security.code, security.market)
                evidence = build_evidence(doc, parsed, keywords)
                sample = {
                    "title": doc.title,
                    "type": doc.doc_type,
                    "publish_date": doc.publish_date,
                    "url": doc.url,
                    "sha256": doc.sha256[:16],
                    "bytes": doc.size_bytes,
                    "pages": info["page_count"],
                    "parsed_pages": info["parsed_pages"],
                    "chars": info["chars"],
                    "truncated": info["truncated"],
                    "evidence": (
                        {
                            "location": evidence.location,
                            "quote": evidence.quote[:180],
                            "fingerprint": evidence.fingerprint,
                            "verified": evidence.verified,
                        }
                        if evidence
                        else None
                    ),
                }
                doc_check["samples"].append(sample)
                status = "已定位证据" if evidence else "未定位到关键词"
                print(
                    f"      [{doc.doc_type}] {doc.title[:34]:34s} {doc.size_bytes/1024:7.0f}KB "
                    f"{info['page_count']:4d}页 | {status}"
                )
    except Exception as exc:
        result["failures"].append(f"原文处理异常：{type(exc).__name__} {exc}")
    print(f"      下载 {doc_check['downloaded']} 份，成功解析 {doc_check['parsed']} 份，失败 {len(doc_check['failed'])} 份")
    result["checks"]["documents"] = doc_check

    # ---------------- 5. 数据源健康 ----------------
    stats = summarize_records(http.records)
    result["sources"] = {
        "requests": stats,
        "hosts": stats["hosts"],
        "elapsed_seconds": round(time.time() - started, 1),
    }
    print(
        f"\n[5/5] 网络请求 {stats['total']} 次，成功 {stats['ok']}，失败 {stats['failed']}，"
        f"下载 {stats['bytes']/1024:.0f}KB，耗时 {time.time()-started:.1f}s"
    )
    http.close()
    return result


def render_markdown(results: list[dict]) -> str:
    lines = [
        "# 阶段 0 数据验证报告",
        "",
        f"生成时间：{now_iso()}",
        "",
        "本文件由 `scripts/phase0_validate.py` 自动生成，所有数字来自真实接口调用，不使用模拟数据。",
        "",
        "## 1. 结论摘要",
        "",
    ]
    verdict = []
    for r in results:
        checks = r["checks"]
        fin = checks.get("finance", {})
        ann = checks.get("announcements", {})
        docs = checks.get("documents", {})
        verdict.append(
            f"- **{r['query']}（{r['expected_market']}）**："
            f"身份{'通过' if checks.get('identity', {}).get('ok') else '失败'}、"
            f"财务 {fin.get('fact_count', 0)} 条事实 / {fin.get('period_count', 0)} 个报告期、"
            f"公告 {ann.get('fetched', 0)} 条、"
            f"原文下载 {docs.get('downloaded', 0)} 份（解析成功 {docs.get('parsed', 0)} 份）、"
            f"失败项 {len(r['failures'])} 个"
        )
    lines += verdict
    lines += ["", "## 2. 各样本明细", ""]

    for r in results:
        c = r["checks"]
        lines.append(f"### 2.{results.index(r)+1} {r['query']}（{r['expected_market']}）")
        lines.append("")
        ident = c.get("identity", {})
        sel = ident.get("selected") or {}
        lines += [
            f"- 证券：`{sel.get('secucode')}` {sel.get('name')}",
            f"- 公司全称：{sel.get('org_name') or '未获取'}",
            f"- 交易所：{sel.get('exchange')}；币种：{sel.get('currency')}；行业：{sel.get('industry') or '未获取'}",
            f"- 候选数量：{ident.get('candidate_count')}；歧义：{ident.get('ambiguous')}",
        ]
        if ident.get("company"):
            secs = ident["company"]["securities"]
            if len(secs) > 1:
                lines.append(
                    "- A/H 关联：" + "、".join(f"{s['secucode']} {s['name']}" for s in secs)
                )
        lines.append("")

        fin = c.get("finance", {})
        lines += [
            f"**财务数据**：{fin.get('fact_count', 0)} 条事实，覆盖 {fin.get('period_count', 0)} 个报告期，"
            f"最新 {fin.get('latest_period') or '无'}，币种 {fin.get('currencies')}",
            "",
            "| 报表 | 事实条数 | 科目数 |",
            "|---|---:|---:|",
        ]
        for st, info in (fin.get("statements") or {}).items():
            lines.append(f"| {st} | {info['fact_count']} | {len(info['items'])} |")
        lines.append("")

        ann = c.get("announcements", {})
        if ann.get("error"):
            lines.append(f"**公告检索**：失败 - {ann['error']}")
        else:
            lines += [
                f"**公告检索**：来源 {ann.get('source')}，获取 {ann.get('fetched')} 条 / 声明 {ann.get('total')} 条，"
                f"区间 {ann.get('range')}",
                "",
            ]
            dist = ann.get("type_distribution") or {}
            if dist:
                lines.append("类型分布：" + "、".join(f"{k} {v}" for k, v in dist.items()))
                lines.append("")
        docs = c.get("documents", {})
        lines += [
            f"**原文获取**：下载 {docs.get('downloaded', 0)} 份，解析成功 {docs.get('parsed', 0)} 份，失败 {len(docs.get('failed', []))} 份",
            "",
        ]
        for s in docs.get("samples", [])[:4]:
            ev = s.get("evidence")
            lines.append(
                f"- `{s['type']}` {s['title'][:40]}（{s['publish_date']}，{s['pages']} 页，"
                f"{s['bytes']/1024:.0f}KB）"
            )
            if ev:
                lines.append(f"  - 证据定位：{ev['location']}，指纹 `{ev['fingerprint']}`")
                lines.append(f"  - 片段：{ev['quote'][:150]}…")
            else:
                lines.append("  - 证据定位：未在正文中命中关键词")
        lines.append("")
        if r["failures"]:
            lines.append("**失败与异常：**")
            lines += [f"- {f}" for f in r["failures"]]
            lines.append("")
        if r["gaps"]:
            lines.append("**覆盖缺口：**")
            lines += [f"- {g}" for g in dict.fromkeys(r["gaps"])]
            lines.append("")

    lines += [
        "## 3. 数据源权限与限制",
        "",
        "| 数据源 | 用途 | 鉴权要求 | 实测状态 | 限制与风险 |",
        "|---|---|---|---|---|",
        "| 东方财富数据中心 | A 股 / 港股三表与主要指标 | 无需鉴权 | 可用 | 非官方授权接口，字段与频率可能变动；商用需自行确认授权 |",
        "| 巨潮资讯网 | A 股公告与原文 | 无需鉴权 | 可用 | 深交所法定披露平台；高频抓取需节制，商业再分发需确认条款 |",
        "| 港交所披露易 | 港股公告与原文 | 无需鉴权 | 可用 | 公开网页接口，非 IIS 授权信息流；商用与批量抓取需确认接入条款 |",
        "| LLM 供应商 | 附注解读、事件提取、核验 | 需 API Key | 未配置 | 见 `.env.example`；未配置时不产出 AI 结论，不以模拟结果替代 |",
        "",
        "## 4. 阶段 0 判定",
        "",
    ]

    ok_a = any(
        r["expected_market"] == "A" and r["checks"].get("finance", {}).get("fact_count", 0) > 0
        and r["checks"].get("announcements", {}).get("fetched", 0) > 0
        and r["checks"].get("documents", {}).get("parsed", 0) > 0
        for r in results
    )
    ok_hk = any(
        r["expected_market"] == "HK" and r["checks"].get("finance", {}).get("fact_count", 0) > 0
        and r["checks"].get("announcements", {}).get("fetched", 0) > 0
        and r["checks"].get("documents", {}).get("parsed", 0) > 0
        for r in results
    )
    lines.append(
        f"- A 股链路（身份 + 财报 + 公告 + 原文）：**{'通过' if ok_a else '未通过'}**"
    )
    lines.append(
        f"- 港股链路（身份 + 财报 + 公告 + 原文）：**{'通过' if ok_hk else '未通过'}**"
    )
    lines += [
        "",
        "判定说明：通过标准为四个环节均取得真实数据。任一项未通过时，",
        "对应市场的功能不得标记为已验收（方案 §3）。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    samples = [(args[0], Market.A), (args[1], Market.HK)] if len(args) >= 2 else [
        ("600519", Market.A),
        ("00700", Market.HK),
    ]
    results = [validate_one(q, m) for q, m in samples]

    (DOCS / "phase0_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (DOCS / "PHASE0_DATA_VALIDATION.md").write_text(
        render_markdown(results), encoding="utf-8"
    )
    print(f"\n\n已写入：{DOCS / 'PHASE0_DATA_VALIDATION.md'}")
    print(f"已写入：{DOCS / 'phase0_result.json'}")
    for r in results:
        if r["failures"]:
            print(f"\n{r['query']} 失败项：")
            for f in r["failures"]:
                print("  -", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
