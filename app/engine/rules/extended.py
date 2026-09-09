"""V1.3 深度检查：只使用已获取事实和可定位的正文，不调用模型计算。

数值阈值是筛查阈值；正文匹配只确认披露内容，不推定违法性质或当前存续。
每项保存输入、排除理由和公式，供报告底稿追溯。
"""
from __future__ import annotations

import math
import re
from datetime import date
from app.core.models import Dimension, PeriodType, RuleStatus as S, Severity as V
from app.core.text import S2T_MAP
from app.engine.rules.base import Rule, RuleContext, fmoney, fnum

FINANCE = ["bank", "insurance", "broker"]
_TRANSLATE = str.maketrans({v: k for k, v in S2T_MAP.items()} | {
    "無": "无", "償": "偿", "還": "还", "內": "内", "認": "认", "識": "识",
    "餘": "余", "額": "额", "壞": "坏", "賬": "账", "險": "险", "顯": "显",
    "標": "标", "準": "准", "則": "则", "遲": "迟", "約": "约", "獨": "独",
    "佔": "占", "專": "专", "項": "项", "終": "终", "斷": "断", "說": "说",
})


def normalized(text):
    return re.sub(r"\s+", "", text).translate(_TRANSLATE)


# Only assertive phrases, not isolated words such as '违约' or '减值'.
SPECS = {
    "SV09": ("债务违约与逾期", r"债务|借款|债券|贷款|本息|利息|偿债",
        r"未能(?:按期|如期|按时)?(?:偿还|偿付|支付|兑付)|无法(?:按期|如期)?(?:偿还|偿付|兑付)|(?:发生|出现|构成)(?:了)?(?:债务)?(?:逾期|违约|交叉违约)|(?:债务|借款|债券|贷款)(?:已经|已)?逾期|(?:违反|触发)(?:了)?(?:借款|债务|贷款)契约",
        r"不存在(?:债务|借款|贷款)?(?:逾期|违约)|未发生(?:债务)?(?:逾期|违约)", V.HIGH),
    "GV07": ("资金占用与违规担保", r"资金占用|占用资金|非经营性占用|违规担保",
        r"(?:存在|发生|发现|涉及)(?:了)?(?:控股股东|实际控制人|关联方)?(?:非经营性)?(?:资金占用|占用资金|违规担保)|(?:资金|资产)被(?:控股股东|实际控制人|关联方).{0,15}占用|(?:控股股东|实际控制人|关联方).{0,12}(?:非经营性占用|占用.{0,6}资金)",
        r"不存在.{0,15}(?:资金占用|占用资金|违规担保)|未发生.{0,15}(?:资金占用|违规担保)", V.HIGH),
    "GV08": ("内部控制重大缺陷", r"内部控制|内控",
        r"(?:内部控制|内控).{0,25}(?:存在重大缺陷|发现重大缺陷|否定意见|无法表示意见)|(?:否定意见|无法表示意见).{0,15}(?:内部控制|内控)",
        r"(?:内部控制|内控).{0,20}(?:不存在重大缺陷|未发现重大缺陷)", V.HIGH),
    "FQ14": ("应收款回收质量", r"应收|合同资产|回款|账龄|坏账",
        r"(?:应收账款|应收款项|其他应收款|合同资产).{0,25}(?:逾期|无法收回|难以收回|回收困难)|(?:账龄|逾期比例).{0,12}(?:显著增加|大幅增加|恶化)|(?:主要客户|客户).{0,15}(?:破产|无力偿还)",
        r"(?:应收账款|应收款项).{0,12}(?:不存在逾期|无逾期)", V.MEDIUM),
    "OP06": ("资产减值与资本化异常线索", r"存货|商誉|在建工程|资本化|减值",
        r"(?:存货|商誉|在建工程).{0,25}(?:减值迹象|严重积压|长期停工|长期停建)|(?:资本化|减值准备).{0,15}(?:不符合|不充分|不足|不合理)|(?:未及时|未足额)(?:计提|确认).{0,8}减值",
        r"(?:商誉|存货|在建工程).{0,12}(?:未发现减值迹象|不存在减值迹象)", V.MEDIUM),
    "OP07": ("关键客户与供应商依赖", r"客户|供应商|合同|销售集中|采购集中",
        r"(?:主要|重大|重要|第一大)(?:客户|供应商).{0,25}(?:终止合作|停止供货|破产|流失)|(?:客户|供应商)(?:集中度|依赖度).{0,15}(?:过高|较高|显著上升)|(?:重大|重要)合同.{0,20}(?:终止|取消)",
        r"不存在对(?:单一|主要)(?:客户|供应商)的(?:重大)?依赖", V.MEDIUM),
}


# Existing financial-industry checks gain an evidence route. A missing regulatory ratio
# still remains unsupported; we never manufacture a ratio or a jurisdiction-wide threshold.
SPECS.update({
    "BK03": ("银行资产质量与拨备", r"不良贷款|拨备", r"不良贷款率.{0,12}(?:显著上升|大幅上升)|拨备覆盖率.{0,12}低于监管要求", r"未出现资产质量恶化", V.MEDIUM),
    "BK04": ("银行资本充足水平", r"资本充足", r"资本充足率.{0,12}(?:低于|不满足|未达到)监管要求", r"资本充足率.{0,12}满足监管要求", V.HIGH),
    "IN03": ("保险偿付能力", r"偿付能力", r"偿付能力.{0,12}(?:不足|不达标|低于监管要求)", r"偿付能力.{0,12}满足监管要求", V.HIGH),
    "IN04": ("保险准备金充足性", r"准备金", r"准备金.{0,12}(?:计提不足|不充足)|未足额计提.{0,10}准备金", r"准备金.{0,12}足额计提", V.MEDIUM),
    "BR03": ("券商净资本与流动性", r"净资本|流动性覆盖率|风险覆盖率", r"(?:净资本|流动性覆盖率|风险覆盖率).{0,12}(?:低于|不满足|未达到)监管要求", r"净资本.{0,12}满足监管要求", V.HIGH),
    "BR04": ("券商信用业务风险", r"融资融券|股票质押|两融", r"(?:融资融券|股票质押|两融).{0,20}(?:发生违约|出现违约|大额减值|回收困难)", r"信用业务未发生违约", V.MEDIUM),
})


def _guard(text, start):
    """Local assertion guard; never infer an actual event from a question or scenario."""
    prefix = text[max(0, start - 24):start]
    return bool(re.search(r"无(?:任何)?逾期|未逾期|非逾期|未.{0,6}出具.{0,6}(?:否定意见|无法表示意见)|不低于|未低于|未构成|(?:不|未)(?:存在|发生|发现|涉及)|无(?:违规担保|资金占用)|非(?:否定意见|逾期)|不存在.{0,12}(?:重大缺陷|减值迹象)|未发现.{0,12}(?:重大缺陷|减值迹象)", text)
                or re.search(r"未|无|不(?:存在|会|构成)|是否|有无|假设|假如|一旦|如果|若|可能|或将|避免|防范|防止|如发生", prefix)
                or re.search(r"是否|有无|假设|假如|一旦|如果|可能|或将|不代表|并不意味着", text))


def text_check(ctx: RuleContext, rid: str):
    name, topic, positive, negative, severity = SPECS[rid]
    # Findings cannot be borrowed from a linked A/H issuer or another company.
    expected_source = "hkexnews" if ctx.market.value == "HK" else "cninfo"
    docs = [d for d in ctx.docs if d.secucode == ctx.security.secucode or
            (d.secucode == ctx.security.code and d.source == expected_source)]
    hits, denials, rejected, reviewed = [], [], [], []
    seen = set()
    for doc in docs:
        p = ctx.parsed.get(doc.doc_id)
        if not p or p.error:
            continue
        reviewed.append(doc.doc_id)
        for page, text in p.pages:
            for part in re.finditer(r"[^。；;！？!?]+[。；;！？!?]?", text):
                quote = part.group().strip()
                # Long PDF tables are not prose assertions. Do not truncate into a false claim.
                if not quote or len(quote) > 850:
                    continue
                n = normalized(quote)
                if not re.search(topic, n):
                    continue
                match = re.search(positive, n)
                # A subject anchor in the same clause is required for formal risk findings.
                subjects = ["本公司", "本集团", "本集团的", "公司及子公司", "公司及其子公司",
                            "控股股东", "实际控制人"]
                subjects += [normalized(s) for s in (ctx.security.name, ctx.security.org_name) if s and len(s) > 2]
                subject_ok = False
                if match:
                    # Page headers and an issuer name far away in a table are not the event subject.
                    window = n[max(0, match.start()-90):match.end()+45]
                    subject_ok = any(s in window for s in subjects)
                    if re.search(r"年度报告|半年度报告|财务报表附注|财务报告附注", window):
                        subject_ok = False
                else:
                    subject_ok = any(s in n for s in subjects)
                third_party = bool(re.search(r"(?:其他|同行|某|上市)公司|举例|例如|案例|法律规定|法规规定|应当|应确保", n))
                template = bool(re.search(r"[□√☑☐✓]|情况的说明|内部信用评级|内部信贷风险分级|账面余额|计提比例|按信用风险特征|违约的定义|定义为|风险分类|(?:客户|供应商)集中度过高或过低", n))
                if template:
                    third_party = True
                if rid == "SV09" and re.search(r"客户|供应商|参股|联营|合营|持有|投资|控股股东|实际控制人", n):
                    third_party = True
                if match and subject_ok and not third_party and not _guard(n, match.start()) and not quote.endswith(("？", "?")):
                    key = (doc.doc_id, page, quote)
                    if key not in seen:
                        seen.add(key)
                        hits.append({"doc_id": doc.doc_id, "page": page, "quote": quote,
                                     "publish_date": doc.publish_date, "title": doc.title})
                        ctx.pending_evidence.append((doc, quote, f"第 {page} 页", [name]))
                elif match:
                    rejected.append({"doc_id": doc.doc_id, "page": page, "reason": "否定、假设、主体不明或第三方案例"})
                if subject_ok and not third_party and re.search(negative, n) and not re.search(r"是否|假如|如果|可能|？|\?", n):
                    denials.append({"doc_id": doc.doc_id, "page": page, "quote": quote})
    ctx.workpapers[rid] = {"method": "same-clause assertion v1; no current-state inference",
                           "reviewed_documents": reviewed, "hits": hits,
                           "negative_disclosures": denials, "excluded": rejected,
                           "limitations": "只检查已解析页段；未命中不等于没有风险；解除需逐事项核实"}
    if hits:
        first = max(hits, key=lambda h: h["publish_date"])
        status = S.RISK if severity is V.HIGH else S.WATCH
        return (status, severity,
                f"已披露{name}信号：共 {len(hits)} 处正文；《{first['title']}》第 {first['page']} 页：{first['quote'][:220]}",
                "仅确认所引披露内容；需核实涉事主体、金额、重要性和后续解除进展，不据此认定当前仍存续或财务造假")
    # Even an explicit denial in one document cannot clear other unparsed disclosures.
    note = f"；取得 {len(denials)} 处否定性披露" if denials else ""
    return (S.INSUFFICIENT, V.UNKNOWN,
            f"已检查 {len(reviewed)} 份已解析正文，未取得足以判断{name}的完整依据{note}",
            "需要对应专项说明、附注及最新进展；未命中关键词不作为无风险结论")


def annual_cashflow(ctx):
    rid = "FQ13"
    periods = ctx.facts.periods(PeriodType.ANNUAL)[:3]
    rows = []
    # Use consolidated total profit consistently, or attributable profit consistently as fallback.
    profit_key = next((k for k in ("net_profit", "net_profit_attributable")
                       if all(ctx.facts.get(k, p) for p in periods)), "")
    missing = []
    for p in periods:
        a, b = ctx.facts.get("ocf", p), ctx.facts.get(profit_key, p)
        if not a or not b or any(f.value is None or not math.isfinite(f.value) for f in (a, b)):
            missing.append(p)
            continue
        if any(f.period_type is not PeriodType.ANNUAL or f.unit != "元" or not f.verified for f in (a, b)) or a.currency != b.currency or a.currency in ("", "未核实", "UNKNOWN"):
            missing.append(p)
            continue
        if a.period_start != b.period_start:
            missing.append(p)
            continue
        if a.period_start and not 360 <= (date.fromisoformat(p) - date.fromisoformat(a.period_start)).days <= 370:
            missing.append(p)
            continue
        rows.append({"period": p, "ocf": a.to_dict(), "profit": b.to_dict()})
    consecutive = len(periods) == 3 and all(
        360 <= (date.fromisoformat(periods[i]) - date.fromisoformat(periods[i+1])).days <= 370 for i in range(2))
    comparable = len({r["ocf"]["currency"] for r in rows}) == 1
    paper = {"inputs": rows, "missing_periods": missing, "consecutive_years": consecutive,
             "formula": "sum(OCF for 3 annual periods) / sum(profit on same basis)",
             "threshold": "cumulative profit > 0 and cash/profit < 0.5; or 3 consecutive OCF < 0",
             "profit_basis": profit_key, "threshold_type": "screening, not regulatory"}
    ctx.workpapers[rid] = paper
    if len(rows) != 3 or not consecutive or not comparable:
        return S.INSUFFICIENT, V.UNKNOWN, "缺少连续三个完整财年、同币种同口径的经营现金流与利润", "不混合年报和中报，不跨缺失年份拼接"
    cash = sum(r["ocf"]["value"] for r in rows)
    profit = sum(r["profit"]["value"] for r in rows)
    paper.update(cumulative_ocf=cash, cumulative_profit=profit, currency=rows[0]["ocf"]["currency"])
    finding = f"{periods[-1]} 至 {periods[0]}，三年累计经营现金流 {fmoney(cash)}、累计利润 {fmoney(profit)}（{paper['currency']}）"
    if all(r["ocf"]["value"] < 0 for r in rows):
        return S.WATCH, V.MEDIUM, finding, "连续三个完整财年经营现金流为负，应核实融资依赖和营运资金占用"
    if profit <= 0:
        return S.NOT_APPLICABLE, V.UNKNOWN, finding, "累计利润非正，不套用利润现金覆盖倍数；亏损另由 FQ08 检查"
    if cash / profit < .5:
        return S.WATCH, V.MEDIUM, finding, "三年累计现金流对利润覆盖不足 0.5 倍；这是筛查线索，需结合行业及扩张周期核实"
    return S.NORMAL, V.LOW, finding, "三年累计现金覆盖未触发本项阈值，不等于全面确认盈利质量"


def maturity_check(ctx):
    """A-share current non-current-liability bucket already includes current leases/bonds.

    HK current borrowings may already include current maturities: never add them again.
    Only explicitly supplied total or complete mutually exclusive components can clear a check.
    """
    rid = "SV10"
    total = ctx.current_fact("debt_due_within_one_year")
    keys = ["short_term_borrowings", "current_noncurrent_liabilities", "other_current_interest_debt"]
    components = [ctx.current_fact(k) for k in keys]
    paper = {"inputs": [f.to_dict() for f in ([total] if total else components) if f],
             "formula": "usable_cash / debt_due_within_one_year",
             "components": keys, "missing": [], "threshold": "<1 watch",
             "limitations": "不把未披露当作零；一年内到期非流动负债不得再重复加到期债券或租赁负债"}
    ctx.workpapers[rid] = paper
    if total is not None:
        debt = total.value
    elif ctx.market.value == "A":
        paper["missing"] = [k for k, f in zip(keys, components) if f is None]
        debt = sum(f.value for f in components) if all(f is not None and f.value is not None for f in components) else None
    else:
        debt = None
        paper["missing"] = ["debt_due_within_one_year（避免港股流动借贷重复相加）"]
    cash = ctx.metrics.get("usable_cash")
    paper.update(usable_cash=cash, debt_due_within_one_year=debt,
                 cash_inputs=[f.to_dict() for k in ("cash", "cash_equivalents", "restricted_cash") if (f := ctx.current_fact(k)) is not None])
    if debt is None and ctx.market.value == "A" and cash is not None and math.isfinite(cash) and cash >= 0:
        known = [f for f in components if f and f.value is not None and math.isfinite(f.value) and f.value >= 0 and f.verified and f.unit == "元"]
        lower_bound = sum(f.value for f in known)
        paper["known_debt_lower_bound"] = lower_bound
        if lower_bound > cash:
            return (S.WATCH, V.MEDIUM,
                    f"已识别一年内债务至少 {fmoney(lower_bound)}，超过可用现金 {fmoney(cash)}；仍有债务组成未披露",
                    "只以可核实组成项形成债务下限，不把缺失项当零；完整到期金额仍待核实")
    if debt is None or not math.isfinite(debt) or debt < 0 or any(f and (f.value is None or f.value < 0 or not f.verified or f.unit != "元") for f in ([total] if total else components)):
        return S.INSUFFICIENT, V.UNKNOWN, "未取得完整、无重复的一年内到期有息债务口径", "保留 SV01 的短期借款检查，但不能用短期借款代替全部到期债务"
    if debt == 0:
        return S.NOT_APPLICABLE, V.UNKNOWN, "已披露的一年内到期有息债务为零", "不计算零分母覆盖率"
    if cash is None or not math.isfinite(cash) or cash < 0:
        return S.INSUFFICIENT, V.UNKNOWN, "缺少经受限资金调整的可用现金", "不使用账面现金替代可用现金"
    ratio = cash / debt
    paper["ratio"] = ratio
    finding = f"可用现金 {fmoney(cash)} / 一年内到期债务 {fmoney(debt)} = {fnum(ratio, '倍')}"
    if ratio < 1:
        return S.WATCH, V.MEDIUM, finding, "静态现金不足以覆盖已披露到期债务；需结合未来经营现金流和已承诺融资安排核实"
    return S.NORMAL, V.LOW, finding, "已披露口径下静态覆盖未触发阈值；不代表未来融资一定可得"


def build_extended_rules():
    dims = {"SV09": Dimension.SOLVENCY, "GV07": Dimension.GOVERNANCE, "GV08": Dimension.GOVERNANCE,
            "FQ14": Dimension.FINANCIAL_QUALITY, "OP06": Dimension.OPERATION, "OP07": Dimension.OPERATION}
    rules = [Rule(rid, spec[0], dims[rid], "已解析正文中的明确披露；未命中不代表不存在", check=lambda ctx, rid=rid: text_check(ctx, rid),
                  exclude_packs=FINANCE if rid in {"FQ14", "OP07"} else []) for rid, spec in SPECS.items() if rid in dims]
    rules.extend([
        Rule("FQ13", "三年累计利润与现金流匹配", Dimension.FINANCIAL_QUALITY, "连续三个同口径完整财年", check=annual_cashflow, exclude_packs=FINANCE),
        Rule("SV10", "未来一年偿债覆盖", Dimension.SOLVENCY, "完整到期债务口径与可用现金", check=maturity_check, exclude_packs=FINANCE),
    ])
    return rules
