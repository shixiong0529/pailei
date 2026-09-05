"""特殊行业规则包：银行、保险、券商、地产。

方案 §6 要求：具体监管阈值在实施时根据适用市场和规则版本核实，不由模型自行生成。
本模块中凡涉及监管口径的指标（资本充足率、偿付能力、净资本等），
当前数据源未提供时一律输出“数据不足，无法判断”，不做估算。
"""

from __future__ import annotations

from app.core.models import Dimension, RuleStatus, Severity
from app.engine.rules.base import Rule, RuleContext, fmoney, fnum

# 行业关键词 → 规则包
INDUSTRY_PACKS: dict[str, list[str]] = {
    "bank": ["银行", "貨幣", "银行Ⅱ", "银行Ⅲ"],
    "insurance": ["保险", "人壽", "人寿", "再保", "保險"],
    "broker": ["证券", "証券", "券商", "投資銀行"],
    "realestate": ["房地产", "地產", "地产", "房地产开发", "物业开发", "房地產"],
}


def classify_industry(industry: str, name: str = "") -> str:
    """根据行业分类与公司简称判断适用规则包。无法判定时归入 general。"""
    text = f"{industry} {name}"
    for pack, keywords in INDUSTRY_PACKS.items():
        if any(k in text for k in keywords):
            return pack
    return "general"


# ---------------------------------------------------------------------- 银行


def _bk01(ctx: RuleContext):
    loans = ctx.facts.latest("bank_loan_advance")
    deposits = ctx.facts.latest("bank_accept_deposit")
    indicator = ctx.metrics.get("bank_loan_deposit_ratio")
    if indicator is not None:
        finding = f"存贷比（数据源主要指标）= {fnum(indicator)}"
        if indicator > 0.85:
            return (RuleStatus.WATCH, Severity.MEDIUM, finding,
                    "存贷比偏高，需结合流动性覆盖率与存款结构判断")
        return (RuleStatus.NORMAL, Severity.LOW, finding, "存贷比处于常规区间")
    if loans and deposits and loans.value and deposits.value:
        ratio = loans.value / deposits.value
        finding = (
            f"发放贷款及垫款 {fmoney(loans.value)}，吸收存款 {fmoney(deposits.value)}，"
            f"存贷比 {fnum(ratio)}"
        )
        if ratio > 0.85:
            return (RuleStatus.WATCH, Severity.MEDIUM, finding, "存贷比偏高，流动性管理压力上升")
        return (RuleStatus.NORMAL, Severity.LOW, finding, "存贷比处于常规区间")
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "缺少贷款与存款科目，无法计算存贷比", "")


def _bk02(ctx: RuleContext):
    nii = ctx.facts.latest("bank_net_interest_income")
    rev = ctx.metrics.get("revenue") or ctx.metrics.get("operating_revenue")
    if nii is None or rev is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                f"利息净收入 {fmoney(nii.value if nii else None)}，营业收入 {fmoney(rev)}", "")
    ratio = nii.value / rev if rev else None
    finding = f"利息净收入 {fmoney(nii.value)}，占营业收入 {fnum(ratio)}"
    if ratio is not None and ratio > 0.80:
        return (RuleStatus.WATCH, Severity.LOW, finding,
                "收入高度依赖利息净收入，中间业务收入占比低，利率市场化下息差承压")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "收入结构未显示过度依赖息差")


def _bk03(ctx: RuleContext):
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "不良贷款率与拨备覆盖率未包含在当前数据源的结构化字段中",
            "")


def _bk04(ctx: RuleContext):
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "资本充足率未包含在当前数据源的结构化字段中，需接入监管报表或年报附注后判断", "")


def build_bank_rules() -> list[Rule]:
    return [
        Rule("BK01", "存贷比", Dimension.SOLVENCY, "贷款与存款匹配度",
             packs=["bank"], check=_bk01),
        Rule("BK02", "利息净收入依赖度", Dimension.OPERATION, "收入结构",
             packs=["bank"], check=_bk02),
        Rule("BK03", "资产质量与拨备", Dimension.FINANCIAL_QUALITY,
             "不良率与拨备覆盖", packs=["bank"], check=_bk03),
        Rule("BK04", "资本充足水平", Dimension.SOLVENCY,
             "资本充足率", packs=["bank"], check=_bk04),
    ]


# ---------------------------------------------------------------------- 保险


def _in01(ctx: RuleContext):
    premium = ctx.facts.latest("ins_earned_premium") or ctx.facts.latest("ins_premium_income")
    if premium is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到已赚保费科目", "")
    return (RuleStatus.NORMAL, Severity.LOW,
            f"已赚保费 {fmoney(premium.value)}（{premium.period_end[:4]}年{premium.period_type.label}）",
            "")


def _in02(ctx: RuleContext):
    claims = ctx.facts.latest("ins_claims_expense")
    premium = ctx.facts.latest("ins_earned_premium")
    if claims is None or premium is None or not premium.value:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                f"赔付支出 {fmoney(claims.value if claims else None)}，已赚保费 {fmoney(premium.value if premium else None)}",
                "")
    ratio = claims.value / premium.value
    finding = f"赔付支出 {fmoney(claims.value)}，占已赚保费 {fnum(ratio)}"
    if ratio > 0.70:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "赔付率偏高，承保端盈利能力承压")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "赔付率处于常规区间")


def _in03(ctx: RuleContext):
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "偿付能力充足率未包含在当前数据源的结构化字段中", "")


def _in04(ctx: RuleContext):
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "保险准备金充足性需读取年报精算与准备金章节，本次未做全文精读", "")


def build_insurance_rules() -> list[Rule]:
    return [
        Rule("IN01", "保费规模", Dimension.OPERATION, "已赚保费",
             packs=["insurance"], check=_in01),
        Rule("IN02", "赔付率", Dimension.FINANCIAL_QUALITY, "赔付支出 / 已赚保费",
             packs=["insurance"], check=_in02),
        Rule("IN03", "偿付能力充足率", Dimension.SOLVENCY, "监管偿付能力指标",
             packs=["insurance"], check=_in03),
        Rule("IN04", "准备金充足性", Dimension.FINANCIAL_QUALITY, "准备金计提",
             packs=["insurance"], check=_in04),
    ]


# ---------------------------------------------------------------------- 券商


def _br01(ctx: RuleContext):
    agent = ctx.facts.latest("broker_agent_trade_security")
    if agent is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                "未获取到代理买卖证券款科目（疑似非券商口径或未披露）", "")
    return (RuleStatus.NORMAL, Severity.LOW,
            f"代理买卖证券款 {fmoney(agent.value)}", "")


def _br02(ctx: RuleContext):
    repo = ctx.facts.latest("broker_sell_repo_finasset")
    assets = ctx.metrics.get("total_assets")
    if repo is None or not assets:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                f"卖出回购金融资产 {fmoney(repo.value if repo else None)}，总资产 {fmoney(assets)}", "")
    ratio = repo.value / assets
    finding = f"卖出回购金融资产 {fmoney(repo.value)}，占总资产 {fnum(ratio)}"
    if ratio > 0.25:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding,
                "回购融资规模占资产比重较高，杠杆水平与市场流动性敏感")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "回购融资占比处于常规区间")


def _br03(ctx: RuleContext):
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "净资本、风险资本准备等券商监管指标未包含在当前数据源中", "")


def _br04(ctx: RuleContext):
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "信用业务（两融、股票质押）规模与风险未包含在当前数据源中", "")


def build_broker_rules() -> list[Rule]:
    return [
        Rule("BR01", "客户资金规模", Dimension.OPERATION, "代理买卖证券款",
             packs=["broker"], check=_br01),
        Rule("BR02", "回购融资杠杆", Dimension.SOLVENCY, "卖出回购 / 总资产",
             packs=["broker"], check=_br02),
        Rule("BR03", "净资本与流动性", Dimension.SOLVENCY, "券商监管指标",
             packs=["broker"], check=_br03),
        Rule("BR04", "信用业务风险", Dimension.SOLVENCY, "两融与股票质押",
             packs=["broker"], check=_br04),
    ]


# ---------------------------------------------------------------------- 地产


def _re01(ctx: RuleContext):
    """剔除预收款后的资产负债率（近似“三道红线”口径之一）。"""
    liab = ctx.metrics.get("total_liabilities")
    assets = ctx.metrics.get("total_assets")
    advance = ctx.facts.latest("advance_receivables")
    if liab is None or not assets:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                f"总负债 {fmoney(liab)}，总资产 {fmoney(assets)}", "")
    adj_liab = liab - (advance.value if advance and advance.value else 0)
    ratio = adj_liab / assets
    finding = (
        f"剔除预收款后的资产负债率 = {fnum(ratio)}；"
        f"（总负债 {fmoney(liab)} - 预收款项 {fmoney(advance.value if advance else None)}）/ 总资产 {fmoney(assets)}"
    )
    if ratio > 0.70:
        return (RuleStatus.RISK, Severity.HIGH, finding,
                "剔除预收后的负债率超过 70%，触及行业监管关注区间，再融资空间受限")
    if ratio > 0.60:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "剔除预收后的负债率偏高")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "剔除预收后的负债率处于常规区间")


def _re02(ctx: RuleContext):
    cash = ctx.metrics.get("cash")
    restricted = ctx.metrics.get("restricted_cash")
    st = ctx.metrics.get("short_term_borrowings")
    if cash is None or st is None or not st:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                f"货币资金 {fmoney(cash)}，短期借款 {fmoney(st)}", "")
    usable = cash - (restricted or 0)
    ratio = usable / st
    finding = (
        f"可自由使用现金 {fmoney(usable)} / 短期借款 {fmoney(st)} = {fnum(ratio, '倍')}"
        + (f"；其中受限资金 {fmoney(restricted)}" if restricted else "")
    )
    if ratio < 1:
        return (RuleStatus.RISK, Severity.HIGH, finding,
                "地产行业预售资金监管严格，受限资金比例高，可动用现金不足覆盖短债时流动性风险显著")
    if ratio < 1.5:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "现金对短债覆盖偏紧")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "现金可覆盖短期借款")


def _re03(ctx: RuleContext):
    inventory = ctx.metrics.get("inventory")
    assets = ctx.metrics.get("total_assets")
    if inventory is None or not assets:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                f"存货 {fmoney(inventory)}，总资产 {fmoney(assets)}", "")
    ratio = inventory / assets
    finding = f"存货（含开发产品）{fmoney(inventory)}，占总资产 {fnum(ratio)}"
    if ratio > 0.60:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding,
                "存货占比高，去化速度决定现金流回笼，需关注项目所在城市与减值计提")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "存货占比处于常规区间")


def _re04(ctx: RuleContext):
    advance = ctx.facts.latest("advance_receivables")
    if advance is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                "未获取到预收款项/合同负债科目，无法判断销售回款前瞻", "")
    series = ctx.facts.series("advance_receivables", limit=3)
    if len(series) >= 2:
        latest_v, prev_v = series[0].value, series[1].value
        if prev_v and latest_v is not None:
            change = (latest_v - prev_v) / abs(prev_v)
            finding = (
                f"预收款项 {fmoney(latest_v)}（{series[0].label}），"
                f"较上期 {fmoney(prev_v)} 变动 {fnum(change)}"
            )
            if change < -0.30:
                return (RuleStatus.WATCH, Severity.MEDIUM, finding,
                        "预收款项（合同负债）大幅下降，通常反映销售回款放缓，是地产现金流的前瞻指标")
            return (RuleStatus.NORMAL, Severity.LOW, finding, "预收款项未出现大幅下滑")
    return (RuleStatus.NORMAL, Severity.LOW,
            f"预收款项 {fmoney(advance.value)}（{advance.period_end[:4]}年）", "")


def build_realestate_rules() -> list[Rule]:
    return [
        Rule("RE01", "剔除预收后的资产负债率", Dimension.SOLVENCY,
             "(总负债-预收)/总资产", packs=["realestate"], check=_re01),
        Rule("RE02", "可动用现金对短债覆盖", Dimension.SOLVENCY,
             "地产版现金覆盖（扣除受限）", packs=["realestate"], check=_re02),
        Rule("RE03", "存货去化压力", Dimension.OPERATION,
             "存货/总资产", packs=["realestate"], check=_re03),
        Rule("RE04", "预收款项变动", Dimension.OPERATION,
             "合同负债趋势", packs=["realestate"], check=_re04),
    ]
