"""通用行业规则包（36 项检查：FQ12 / SV8 / GV6 / RG5 / OP5）。

每条规则只输出五种结论之一；缺少数据时输出“数据不足，无法判断”，
绝不因为没查到就输出“未发现异常”。
"""

from __future__ import annotations

from typing import Optional

from app.core.models import Dimension, DisclosureDoc, RuleStatus, Severity
from app.engine.rules.base import Rule, RuleContext, fmoney, fnum

PACK = ["general"]

# 财务结构类规则对银行、保险、券商不适用：
# 这些行业的“负债”即经营对象，套用普通企业标准会产生大量误报
FINANCIAL_PACKS = ["bank", "insurance", "broker"]


def _dim_doc_keywords(dimension: Dimension) -> tuple[list[str], list[str]]:
    return {
        Dimension.FINANCIAL_QUALITY: (
            ["年报", "半年报", "审计", "财务更正", "季报"],
            ["应收账款", "存货", "现金流", "减值", "非经常", "会计政策"],
        ),
        Dimension.SOLVENCY: (
            ["年报", "半年报", "季报", "审计"],
            ["借款", "负债", "现金", "担保", "债券"],
        ),
        Dimension.GOVERNANCE: (
            ["高管变动", "审计机构", "股权质押", "关联交易", "股东减持", "股东增持", "担保"],
            ["辞职", "聘任", "审计", "质押", "冻结", "关联交易", "减持"],
        ),
        Dimension.REGULATORY: (
            ["监管问询", "监管处罚", "监管调查", "诉讼", "资产冻结", "上市地位"],
            ["问询", "关注", "处罚", "立案", "调查", "诉讼", "仲裁", "冻结", "退市"],
        ),
        Dimension.OPERATION: (
            ["年报", "半年报", "业绩", "业绩预告", "盈利警告", "季报"],
            ["经营", "业绩", "亏损", "持续经营", "商誉", "减值"],
        ),
    }.get(dimension, ([], []))


# ------------------------------------------------------------------ 财务质量


def _r_fq01(ctx: RuleContext):
    ratio = ctx.metrics.get("ocf_to_net_profit")
    ocf = ctx.metrics.get("ocf")
    np_ = ctx.metrics.get("net_profit")
    finding = (
        f"经营现金流 / 净利润 = {fnum(ratio)}；经营现金流 {fmoney(ocf)}，净利润 {fmoney(np_)}"
    )
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if np_ is not None and np_ <= 0:
        return (RuleStatus.NOT_APPLICABLE, Severity.UNKNOWN, finding,
                "净利润非正，现金流对盈利的覆盖倍数不适用；亏损和现金净流出分别由 FQ08、FQ02、OP05 检查")
    if ratio < 0:
        return (
            RuleStatus.RISK, Severity.HIGH, finding,
            "净利润为正而经营现金流为负，利润缺乏现金支撑，是财务质量最常见的预警信号之一",
        )
    if ratio < 0.5:
        return (
            RuleStatus.WATCH, Severity.MEDIUM, finding,
            "经营现金流显著低于净利润，需核实是否存在应收账款或存货占用增加的赊销扩张",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "经营现金流对利润的覆盖处于正常区间")


def _r_fq02(ctx: RuleContext):
    ocf = ctx.metrics.get("ocf")
    finding = f"经营活动现金流净额 {fmoney(ocf)}"
    if ocf is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ocf < 0:
        return (
            RuleStatus.RISK, Severity.HIGH, finding,
            "经营活动现金净流出，主营业务自身造血能力为负，依赖外部融资或资产处置维持运转",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "经营活动现金净流入")


def _materiality(ctx: RuleContext, item_key: str, threshold: float = 0.02) -> Optional[bool]:
    """重要性判断：科目占总资产比重过低时，增速类指标不具备经济意义。

    返回 None 表示无法判断（缺少总资产或科目数据），此时不做重要性过滤。
    """
    ratio = ctx.metrics.get("ar_to_assets" if item_key == "accounts_receivable" else f"{item_key}_to_assets")
    if ratio is None:
        return None
    return ratio >= threshold


def _r_fq03(ctx: RuleContext):
    gap = ctx.metrics.get("ar_growth_minus_revenue_growth")
    ar_yoy = ctx.metrics.get("ar_yoy")
    rev_yoy = ctx.metrics.get("revenue_yoy")
    finding = f"应收账款同比 {fnum(ar_yoy)}，营业收入同比 {fnum(rev_yoy)}"
    if gap is not None:
        finding += f"，增速差 {fnum(gap)}"
    if gap is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    # 应收账款占总资产极低时，增速差异不具备经济意义
    if _materiality(ctx, "accounts_receivable") is False:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            finding + f"；但应收账款仅占总资产 {fnum(ctx.metrics.get('ar_to_assets'))}，"
            "不具备重要性，增速差异不形成风险判断",
            "",
        )
    if gap > 0.30:
        return (
            RuleStatus.RISK, Severity.MEDIUM, finding,
            "应收账款增速显著快于收入，可能指向放宽信用政策、渠道压货或收入确认偏激进",
        )
    if gap > 0.10:
        return (
            RuleStatus.WATCH, Severity.LOW, finding,
            "应收账款增速快于收入，建议核销政策与主要客户回款情况",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "应收账款增速与收入基本匹配")


def _r_fq04(ctx: RuleContext):
    ratio = ctx.metrics.get("ar_to_revenue")
    finding = f"应收账款 / 营业收入 = {fnum(ratio)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio > 0.60:
        return (
            RuleStatus.WATCH, Severity.MEDIUM, finding,
            "应收账款占收入比重偏高，回款周期长会放大坏账与资金占用风险",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "应收账款占收入比重处于常规区间")


def _r_fq05(ctx: RuleContext):
    gap = ctx.metrics.get("inventory_growth_minus_revenue_growth")
    inv_yoy = ctx.metrics.get("inventory_yoy")
    rev_yoy = ctx.metrics.get("revenue_yoy")
    finding = f"存货同比 {fnum(inv_yoy)}，营业收入同比 {fnum(rev_yoy)}"
    if gap is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    # 重要性判断：存货占总资产极低时，增速差异不具备经济意义
    materiality = _materiality(ctx, "inventory", threshold=0.02)
    if materiality is not None and not materiality:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            finding + f"；但存货仅占总资产 {fnum(ctx.metrics.get('inventory_to_assets'))}，"
            "不具备重要性，增速差异不形成风险判断",
            "",
        )
    if gap > 0.30:
        return (
            RuleStatus.RISK, Severity.MEDIUM, finding,
            "存货增速显著快于收入，可能指向需求走弱、滞销或未来减值压力",
        )
    if gap > 0.10:
        return (RuleStatus.WATCH, Severity.LOW, finding, "存货增速快于收入，需关注周转与跌价准备")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "存货增速与收入基本匹配")


def _r_fq06(ctx: RuleContext):
    ratio = ctx.metrics.get("nonrecurring_ratio")
    np_ = ctx.metrics.get("net_profit")
    deducted = ctx.metrics.get("net_profit_deducted")
    finding = (
        f"非经常性损益占净利润 {fnum(ratio)}；净利润 {fmoney(np_)}，扣非净利润 {fmoney(deducted)}"
    )
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio > 0.50:
        return (
            RuleStatus.RISK, Severity.MEDIUM, finding,
            "过半利润来自非经常性损益，主业盈利能力被显著高估，可持续性存疑",
        )
    if ratio > 0.30:
        return (
            RuleStatus.WATCH, Severity.MEDIUM, finding,
            "非经常性收益对利润贡献较大，需核实构成（政府补助、资产处置、投资收益等）",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "利润主要来自经常性经营损益")


def _r_fq07(ctx: RuleContext):
    deducted = ctx.metrics.get("net_profit_deducted")
    np_ = ctx.metrics.get("net_profit")
    finding = f"扣非净利润 {fmoney(deducted)}，净利润 {fmoney(np_)}"
    if deducted is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if deducted < 0 <= (np_ or 0):
        return (
            RuleStatus.RISK, Severity.HIGH, finding,
            "扣非后亏损而报表盈利，主业实际处于亏损状态，靠非经常性项目扭亏",
        )
    if deducted < 0:
        return (
            RuleStatus.WATCH, Severity.MEDIUM, finding,
            "扣非净利润为负，主业尚未实现盈利",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "扣非净利润为正")


def _r_fq08(ctx: RuleContext):
    np_ = ctx.metrics.get("net_profit")
    rev = ctx.metrics.get("revenue")
    margin = ctx.metrics.get("net_margin")
    finding = f"净利润 {fmoney(np_)}，净利率 {fnum(margin)}"
    if np_ is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if np_ < 0:
        return (
            RuleStatus.RISK, Severity.HIGH, finding,
            "报告期净利润为负，公司处于亏损状态",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "报告期净利润为正")


def _r_fq09(ctx: RuleContext):
    yoy = ctx.metrics.get("revenue_yoy")
    finding = f"营业收入同比 {fnum(yoy)}"
    if yoy is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if yoy < -0.30:
        return (
            RuleStatus.RISK, Severity.MEDIUM, finding,
            "营业收入同比下滑超过 30%，需核实是行业周期、竞争格局还是公司自身经营问题",
        )
    if yoy < -0.10:
        return (RuleStatus.WATCH, Severity.LOW, finding, "营业收入同比下滑，需关注趋势是否延续")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "收入未出现明显下滑")


def _r_fq10(ctx: RuleContext):
    yoy = ctx.metrics.get("net_profit_yoy")
    finding = f"净利润同比 {fnum(yoy)}"
    if yoy is None:
        return (
            RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            finding + ("（含负利润基数，普通同比口径不适用）" if ctx.metrics.get("net_profit") is not None else ""),
            "",
        )
    if yoy < -0.50:
        return (
            RuleStatus.RISK, Severity.MEDIUM, finding,
            "净利润同比下滑超过 50%，盈利质量出现明显恶化",
        )
    if yoy < -0.20:
        return (RuleStatus.WATCH, Severity.LOW, finding, "净利润同比下滑明显")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "净利润未出现大幅下滑")


def _r_fq11(ctx: RuleContext):
    ratio = ctx.metrics.get("cash_from_sales_to_revenue")
    finding = f"销售商品收到的现金 / 营业收入 = {fnum(ratio)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio < 0.80:
        return (
            RuleStatus.WATCH, Severity.MEDIUM, finding,
            "销售收现比偏低，收入转化为现金的效率不足，需结合应收与票据核实",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "销售收现比处于正常水平")


def _r_fq12(ctx: RuleContext):
    """财务重述 / 追溯调整：以公告为准，不依赖财务指标。"""
    hits = ctx.evidence.has_any(["更正", "追溯", "差错"])
    docs = ctx.docs_of_type("财务更正")
    if docs:
        titles = "、".join(d.title for d in docs[:3])
        return (
            RuleStatus.RISK, Severity.MEDIUM,
            f"检索到 {len(docs)} 份财务更正/追溯调整类公告：{titles}",
            "财务重述意味着此前披露的财务数据不再可靠，需核实更正范围与原因",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"在已获取的 {len(ctx.docs)} 份公告中未发现财务更正或追溯调整类文件",
            "",
        )
    return (
        RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
        "未获取到公告清单，无法判断是否发生财务重述", "",
    )


# ------------------------------------------------------------------ 偿债能力


def _r_sv01(ctx: RuleContext):
    ratio = ctx.metrics.get("cash_to_short_debt")
    cash = ctx.metrics.get("usable_cash")
    st = ctx.metrics.get("short_term_borrowings")
    finding = f"（货币资金 - 受限资金）/ 短期借款 = {fnum(ratio, '倍')}；可用现金 {fmoney(cash)}，短期借款 {fmoney(st)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio < 1:
        return (
            RuleStatus.RISK, Severity.HIGH, finding,
            "可自由使用现金不足以覆盖短期借款，短期偿付依赖再融资或经营回款",
        )
    if ratio < 1.5:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "现金对短期债务的覆盖偏紧")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "现金可覆盖短期借款")


def _r_sv02(ctx: RuleContext):
    ratio = ctx.metrics.get("debt_ratio")
    finding = f"资产负债率 = {fnum(ratio)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio > 0.80:
        return (RuleStatus.RISK, Severity.MEDIUM, finding, "资产负债率显著偏高，财务杠杆高")
    if ratio > 0.70:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "资产负债率偏高，需结合行业均值判断")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "资产负债率处于常规区间")


def _r_sv03(ctx: RuleContext):
    ratio = ctx.metrics.get("current_ratio")
    finding = f"流动比率 = {fnum(ratio, '倍')}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio < 1:
        return (RuleStatus.RISK, Severity.MEDIUM, finding, "流动资产不足以覆盖流动负债，存在流动性缺口")
    if ratio < 1.2:
        return (RuleStatus.WATCH, Severity.LOW, finding, "流动比率偏紧")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "流动比率正常")


def _r_sv04(ctx: RuleContext):
    ratio = ctx.metrics.get("interest_coverage")
    op_profit = ctx.metrics.get("operating_profit")
    fin_exp = ctx.current_fact("finance_expense") or ctx.current_fact("finance_cost")
    fin_value = fin_exp.value if fin_exp else None
    finding = f"利息保障倍数 = {fnum(ratio, '倍')}"
    # 财务费用为负意味着利息净收入而非净支出，此时该倍数没有经济含义
    if fin_value is not None and fin_value <= 0:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"财务费用为 {fmoney(fin_value)}（为负，即利息净收入大于利息支出），"
            f"利息保障倍数不适用；营业利润 {fmoney(op_profit)}",
            "公司处于净利息收入状态，付息压力不是当前矛盾，本项不适用普通企业的利息保障判定",
        )
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio < 1:
        return (RuleStatus.RISK, Severity.HIGH, finding, "营业利润不足以覆盖利息支出，付息能力承压")
    if ratio < 2:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "利息保障倍数偏低")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "利息保障倍数处于安全区间")


def _r_sv05(ctx: RuleContext):
    ratio = ctx.metrics.get("debt_to_assets_ex_cash")
    finding = f"有息负债 / 总资产 = {fnum(ratio)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio > 0.50:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "有息负债占总资产比重较高")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "有息负债占比处于常规区间")


def _r_sv06(ctx: RuleContext):
    fcf = ctx.metrics.get("fcf_proxy")
    ocf = ctx.metrics.get("ocf")
    capex = ctx.metrics.get("capex")
    finding = f"经营现金流 {fmoney(ocf)} - 资本开支 {fmoney(capex)} = {fmoney(fcf)}"
    if fcf is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if fcf < 0:
        return (
            RuleStatus.WATCH, Severity.MEDIUM, finding,
            "经营现金流不足以覆盖资本开支，扩张依赖外部融资，需关注投资回报与融资条件",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "经营现金流可覆盖当期资本开支")


def _r_sv07(ctx: RuleContext):
    restricted = ctx.metrics.get("restricted_cash")
    cash = ctx.metrics.get("cash")
    if cash is None or cash <= 0 or restricted is None:
        return (
            RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            f"受限资金 {fmoney(restricted)}，货币资金 {fmoney(cash)}", "",
        )
    ratio = (restricted / cash) if (restricted is not None and cash) else 0.0
    finding = f"受限资金 {fmoney(restricted)}，占货币资金 {fnum(ratio)}"
    if ratio > 0.50:
        return (
            RuleStatus.RISK, Severity.MEDIUM, finding,
            "过半货币资金为受限资金，不能用于自由偿债，账面现金的可用性被高估",
        )
    if ratio > 0.20:
        return (RuleStatus.WATCH, Severity.LOW, finding, "存在一定比例受限资金")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "受限资金占比不高")


def _r_sv08(ctx: RuleContext):
    docs = ctx.docs_of_type("担保", "资产冻结")
    if docs:
        return (
            RuleStatus.WATCH, Severity.MEDIUM,
            f"检索到 {len(docs)} 份担保或资产冻结相关公告：" + "、".join(d.title for d in docs[:3]),
            "对外担保与资产冻结会形成表外或受限的偿付义务，需核实余额与代偿风险",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，其中未发现担保或资产冻结类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断担保与冻结情况", "")


# ------------------------------------------------------------------ 公司治理


def _r_gv01(ctx: RuleContext):
    """审计机构变更。

    注意区分两种性质完全不同的公告：
    - 变更/改聘/新聘会计师事务所 → 值得关注；
    - 续聘/履职评估/监督职责报告 → 恰恰说明审计机构保持稳定。
    把“续聘”当成“变更”是典型的误报。
    """
    docs = ctx.docs_of_type("审计机构")
    if not docs:
        if ctx.docs:
            return (
                RuleStatus.NORMAL, Severity.LOW,
                f"已获取 {len(ctx.docs)} 份公告，未发现与会计师事务所有关的文件"
                + (f"；当前审计机构为 {ctx.security.auditor}" if ctx.security.auditor else ""),
                "",
            )
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                "未获取到公告清单，无法判断审计机构是否变更", "")

    change_words = ("变更会计师事务所", "改聘", "更换会计师事务所", "变更审计机构")
    routine_words = ("续聘", "履职", "监督职责", "审计委员会", "选聘", "招标")
    changes, routines = [], []
    for d in docs:
        title = d.title or ""
        if any(w in title for w in routine_words) and not any(
            w in title for w in change_words
        ):
            routines.append(d)
        elif "聘任会计师事务所" in title or any(w in title for w in change_words):
            changes.append(d)
        else:
            routines.append(d)

    auditor_note = f"；当前审计机构为 {ctx.security.auditor}" if ctx.security.auditor else ""

    if changes:
        return (
            RuleStatus.WATCH, Severity.MEDIUM,
            f"检索到 {len(changes)} 份审计机构变更/新聘公告："
            + "、".join(d.title for d in changes[:3]) + auditor_note,
            "审计机构变更需核实变更原因、时点及是否涉及审计意见分歧",
        )
    return (
        RuleStatus.NORMAL, Severity.LOW,
        f"检索到 {len(routines)} 份会计师事务所相关公告，均为续聘或履职评估类"
        + ("：" + "、".join(d.title for d in routines[:3]) if routines else "")
        + auditor_note,
        "续聘与履职评估类公告表明审计机构未发生变更，不构成治理风险信号",
    )


def _r_gv02(ctx: RuleContext):
    docs = ctx.docs_of_type("高管变动")
    if docs:
        return (
            RuleStatus.WATCH, Severity.LOW,
            f"检索到 {len(docs)} 份董事或高管变动公告：" + "、".join(d.title for d in docs[:3]),
            "关键人员变动需结合离职原因与继任安排判断，频繁变动通常值得关注",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现董事或高管变动类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断高管变动情况", "")


def _r_gv03(ctx: RuleContext):
    docs = ctx.docs_of_type("股权质押")
    if docs:
        return (
            RuleStatus.WATCH, Severity.MEDIUM,
            f"检索到 {len(docs)} 份股权质押或冻结相关公告：" + "、".join(d.title for d in docs[:3]),
            "控股股东高比例质押在股价下跌时可能引发平仓与控制权不稳定",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现股权质押或冻结类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断股权质押情况", "")


def _r_gv04(ctx: RuleContext):
    docs = ctx.docs_of_type("关联交易")
    if docs:
        return (
            RuleStatus.WATCH, Severity.LOW,
            f"检索到 {len(docs)} 份关联交易公告：" + "、".join(d.title for d in docs[:3]),
            "关联交易需核实定价公允性与是否构成资金占用",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现关联交易类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断关联交易情况", "")


def _r_gv05(ctx: RuleContext):
    docs = ctx.docs_of_type("股东减持")
    if docs:
        return (
            RuleStatus.WATCH, Severity.LOW,
            f"检索到 {len(docs)} 份股东减持公告：" + "、".join(d.title for d in docs[:3]),
            "主要股东减持需结合减持规模与原因判断，若为控股股东大额减持应重点关注",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现股东减持类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断股东减持情况", "")


def _r_gv06(ctx: RuleContext):
    controller = ctx.security.profile.get("controller") or ""
    if not controller:
        return (
            RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "未能获取实际控制人信息（数据源未返回该项）", "",
        )
    return (
        RuleStatus.NORMAL, Severity.LOW,
        f"实际控制人：{controller}；董事长：{ctx.security.profile.get('chairman') or '未获取'}",
        "",
    )


# ------------------------------------------------------------------ 监管法律


def _r_rg01(ctx: RuleContext):
    docs = ctx.docs_of_type("监管调查", "监管处罚")
    if docs:
        return (
            RuleStatus.RISK, Severity.HIGH,
            f"检索到 {len(docs)} 份立案调查或行政处罚相关公告：" + "、".join(d.title for d in docs[:3]),
            "监管立案或处罚通常伴随财务或信息披露问题，需核实进展、金额与是否触及重大违法情形",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现立案调查或行政处罚类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断监管处罚情况", "")


def _r_rg02(ctx: RuleContext):
    docs = ctx.docs_of_type("诉讼")
    if docs:
        return (
            RuleStatus.WATCH, Severity.MEDIUM,
            f"检索到 {len(docs)} 份诉讼或仲裁相关公告：" + "、".join(d.title for d in docs[:3]),
            "重大诉讼需核实涉诉金额、计提预计负债情况与败诉对现金流的潜在影响",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现诉讼或仲裁类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断诉讼情况", "")


def _r_rg03(ctx: RuleContext):
    docs = ctx.docs_of_type("监管问询")
    if docs:
        return (
            RuleStatus.WATCH, Severity.MEDIUM,
            f"检索到 {len(docs)} 份监管问询或关注函公告：" + "、".join(d.title for d in docs[:3]),
            "交易所问询通常针对财务异常或披露不充分，需核实公司回复与后续整改",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现监管问询类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断监管问询情况", "")


def _r_rg04(ctx: RuleContext):
    docs = ctx.docs_of_type("上市地位")
    if docs:
        return (
            RuleStatus.RISK, Severity.HIGH,
            f"检索到 {len(docs)} 份涉及上市地位、停牌或清盘的公告：" + "、".join(d.title for d in docs[:3]),
            "上市地位相关事项对流动性与估值有直接影响，需核实最新进展",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现涉及上市地位的文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断上市地位风险", "")


def _r_rg05(ctx: RuleContext):
    docs = ctx.docs_of_type("资产冻结")
    if docs:
        return (
            RuleStatus.RISK, Severity.HIGH,
            f"检索到 {len(docs)} 份资产冻结相关公告：" + "、".join(d.title for d in docs[:3]),
            "资产被冻结会直接限制经营与偿债能力，通常伴随债务违约或诉讼保全",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现资产冻结类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断资产冻结情况", "")


# ------------------------------------------------------------------ 经营行业


def _r_op01(ctx: RuleContext):
    docs = ctx.docs_of_type("盈利警告", "业绩预告")
    if docs:
        return (
            RuleStatus.WATCH, Severity.MEDIUM,
            f"检索到 {len(docs)} 份盈利警告或业绩预告：" + "、".join(d.title for d in docs[:3]),
            "公司主动发布的盈利预警是经营恶化的直接信号，需核实原因与是否持续",
        )
    if ctx.docs:
        return (
            RuleStatus.NORMAL, Severity.LOW,
            f"已获取 {len(ctx.docs)} 份公告，未发现盈利警告或业绩预告类文件", "",
        )
    return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断业绩预警情况", "")


def _r_op02(ctx: RuleContext):
    """审计意见类型：需要读取审计报告正文，指标层无法判断。

    直接全文搜索“保留意见”会误报（半年报固定勾选项、
    标准无保留意见中的持续经营免责句都会命中关键词），
    因此改用否定式敏感的专用识别函数。
    """
    from app.data.pdftext import scan_audit_opinions

    audit_docs = ctx.docs_of_type("审计") or ctx.docs_of_type("年报", "半年报")
    if not audit_docs:
        if ctx.docs:
            return (
                RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                "已获取的公告中未包含审计报告类文件，无法判断审计意见类型", "",
            )
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "未获取到公告清单，无法判断审计意见", "")

    scanned = [(d, ctx.parsed[d.doc_id]) for d in audit_docs
               if d.doc_id in ctx.parsed and not ctx.parsed[d.doc_id].error
               and ctx.parsed[d.doc_id].full_text.strip()]
    if not scanned:
        return (
            RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            f"找到 {len(audit_docs)} 份审计类文件，但均未完成正文解析，无法判断意见类型", "",
        )

    hits = []
    for doc, pdoc in scanned:
        for hit in scan_audit_opinions(pdoc):
            hits.append((doc, hit))

    if hits:
        doc, hit = hits[0]
        severity = Severity.HIGH if hit["severity"] == "high" else Severity.HIGH
        ctx.pending_evidence.append(
            (doc, str(hit["quote"]), f"第 {hit['page']} 页", ["审计意见"])
        )
        return (
            RuleStatus.RISK, severity,
            f"《{doc.title}》第 {hit['page']} 页出现「{hit['label']}」表述，命中原文：{hit['matched']}",
            "非标准无保留意见或持续经营重大不确定性，是财务报告可靠性的重大警示；"
            "该结论由否定式敏感匹配得出，仍建议人工复核审计报告意见段",
        )
    from app.data.pdftext import has_audit_opinion_section
    if not any(has_audit_opinion_section(p) for _, p in scanned):
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
                "已解析正文，但未定位到明确的审计意见段", "未检出关键词不能作为标准审计意见的依据")
    return (
        RuleStatus.NORMAL, Severity.LOW,
        f"已解析 {len(scanned)} 份审计/定期报告正文，未检出非标准审计意见的断言式表述",
        "已排除半年报固定勾选项与标准审计报告中的持续经营免责句等模板化表述",
    )


def _r_op03(ctx: RuleContext):
    """资本开支与投资强度。"""
    capex = ctx.metrics.get("capex")
    ocf = ctx.metrics.get("ocf")
    assets = ctx.metrics.get("total_assets")
    ratio = None
    if capex is not None and assets:
        ratio = capex / assets
    finding = f"购建长期资产支付现金 {fmoney(capex)}，占总资产 {fnum(ratio)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "")
    if ratio > 0.15:
        return (
            RuleStatus.WATCH, Severity.LOW, finding,
            "资本开支强度较高，需关注项目回报周期、在建工程转固与折旧压力",
        )
    return (RuleStatus.NORMAL, Severity.LOW, finding, "资本开支强度处于常规水平")


def _r_op04(ctx: RuleContext):
    """商誉与减值：数据源未提供商誉科目时明确标注不足。"""
    goodwill = ctx.current_fact("goodwill")
    if goodwill is None:
        return (
            RuleStatus.INSUFFICIENT, Severity.UNKNOWN,
            "当前数据源未提供商誉科目，无法判断商誉减值风险",
            "",
        )
    assets = ctx.metrics.get("total_assets")
    ratio = (goodwill.value / assets) if assets else None
    finding = f"商誉 {fmoney(goodwill.value)}，占总资产 {fnum(ratio)}"
    if ratio is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, finding, "缺少有效的总资产口径")
    if ratio > 0.20:
        return (RuleStatus.WATCH, Severity.MEDIUM, finding, "商誉占资产比重较高，存在减值压力")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "商誉占比不高")


def _r_op05(ctx: RuleContext):
    """持续经营：以现金流与盈利双重信号判断。"""
    ocf = ctx.metrics.get("ocf")
    np_ = ctx.metrics.get("net_profit")
    equity = ctx.metrics.get("total_equity")
    if ocf is None and np_ is None:
        return (RuleStatus.INSUFFICIENT, Severity.UNKNOWN, "缺少现金流与利润数据，无法判断", "")
    both_negative = (ocf is not None and ocf < 0) and (np_ is not None and np_ < 0)
    finding = f"经营现金流 {fmoney(ocf)}，净利润 {fmoney(np_)}，所有者权益 {fmoney(equity)}"
    if both_negative:
        return (
            RuleStatus.RISK, Severity.HIGH, finding,
            "经营现金流与净利润同时为负，持续经营能力依赖外部支持，需核实融资安排与在手现金",
        )
    if equity is not None and equity < 0:
        return (RuleStatus.RISK, Severity.HIGH, finding, "所有者权益为负，已出现资不抵债")
    return (RuleStatus.NORMAL, Severity.LOW, finding, "未发现持续经营的双重负面信号")


def build_general_rules() -> list[Rule]:
    return [
        Rule("FQ01", "利润与经营现金流背离", Dimension.FINANCIAL_QUALITY,
             "比较经营现金流与净利润的匹配程度",
             requires=["metric:ocf", "metric:net_profit"], packs=PACK, check=_r_fq01),
        Rule("FQ02", "经营现金流为负", Dimension.FINANCIAL_QUALITY,
             "判断主营业务是否净流出", requires=["metric:ocf"], packs=PACK, check=_r_fq02),
        Rule("FQ03", "应收账款增速快于收入", Dimension.FINANCIAL_QUALITY,
             "应收与收入增速差异", requires=["metric:ar_yoy", "metric:revenue_yoy"],
             packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_fq03),
        Rule("FQ04", "应收账款占收入比重过高", Dimension.FINANCIAL_QUALITY,
             "应收占收入比", requires=["metric:ar_to_revenue"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_fq04),
        Rule("FQ05", "存货增速快于收入", Dimension.FINANCIAL_QUALITY,
             "存货与收入增速差异", requires=["metric:inventory_yoy", "metric:revenue_yoy"],
             packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_fq05),
        Rule("FQ06", "非经常性损益依赖", Dimension.FINANCIAL_QUALITY,
             "非经常损益占净利润比", requires=["metric:net_profit_deducted", "metric:net_profit"],
             packs=PACK, check=_r_fq06),
        Rule("FQ07", "扣非净利润为负", Dimension.FINANCIAL_QUALITY,
             "扣非后是否亏损", requires=["metric:net_profit_deducted"], packs=PACK, check=_r_fq07),
        Rule("FQ08", "报告期净利润为负", Dimension.FINANCIAL_QUALITY,
             "是否亏损", requires=["metric:net_profit"], packs=PACK, check=_r_fq08),
        Rule("FQ09", "营业收入大幅下滑", Dimension.FINANCIAL_QUALITY,
             "收入同比", requires=["metric:revenue_yoy"], packs=PACK, check=_r_fq09),
        Rule("FQ10", "净利润大幅下滑", Dimension.FINANCIAL_QUALITY,
             "净利润同比", requires=["metric:net_profit_yoy"], packs=PACK, check=_r_fq10),
        Rule("FQ11", "销售收现比偏低", Dimension.FINANCIAL_QUALITY,
             "收现比", requires=["metric:cash_from_sales_to_revenue"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_fq11),
        Rule("FQ12", "财务重述或追溯调整", Dimension.FINANCIAL_QUALITY,
             "是否存在更正类公告", packs=PACK, check=_r_fq12),
        Rule("SV01", "现金无法覆盖短期借款", Dimension.SOLVENCY,
             "可用现金对短债覆盖", requires=["metric:cash_to_short_debt"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_sv01),
        Rule("SV02", "资产负债率过高", Dimension.SOLVENCY,
             "杠杆水平", requires=["metric:debt_ratio"], packs=PACK, exclude_packs=FINANCIAL_PACKS + ["realestate"], check=_r_sv02),
        Rule("SV03", "流动比率过低", Dimension.SOLVENCY,
             "短期流动性", requires=["metric:current_ratio"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_sv03),
        Rule("SV04", "利息保障倍数不足", Dimension.SOLVENCY,
             "付息能力", requires=["metric:interest_coverage"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_sv04),
        Rule("SV05", "有息负债占比偏高", Dimension.SOLVENCY,
             "有息负债/总资产", requires=["metric:debt_to_assets_ex_cash"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_sv05),
        Rule("SV06", "经营现金流无法覆盖资本开支", Dimension.SOLVENCY,
             "自由现金流", requires=["metric:fcf_proxy"], packs=PACK, check=_r_sv06),
        Rule("SV07", "受限资金占比过高", Dimension.SOLVENCY,
             "现金可用性", requires=["metric:cash"], packs=PACK, check=_r_sv07),
        Rule("SV08", "对外担保与资产受限", Dimension.SOLVENCY,
             "表外义务", packs=PACK, check=_r_sv08),
        Rule("GV01", "审计机构变更", Dimension.GOVERNANCE,
             "审计机构稳定性", packs=PACK, check=_r_gv01),
        Rule("GV02", "董事或高管变动", Dimension.GOVERNANCE,
             "关键人员稳定性", packs=PACK, check=_r_gv02),
        Rule("GV03", "股权质押或冻结", Dimension.GOVERNANCE,
             "股东层面风险", packs=PACK, check=_r_gv03),
        Rule("GV04", "关联交易", Dimension.GOVERNANCE,
             "关联交易与资金占用", packs=PACK, check=_r_gv04),
        Rule("GV05", "股东减持", Dimension.GOVERNANCE,
             "主要股东行为", packs=PACK, check=_r_gv05),
        Rule("GV06", "控制权与治理结构", Dimension.GOVERNANCE,
             "实际控制人披露", packs=PACK, check=_r_gv06),
        Rule("RG01", "立案调查或行政处罚", Dimension.REGULATORY,
             "监管处罚", packs=PACK, check=_r_rg01),
        Rule("RG02", "重大诉讼或仲裁", Dimension.REGULATORY,
             "诉讼风险", packs=PACK, check=_r_rg02),
        Rule("RG03", "监管问询或关注函", Dimension.REGULATORY,
             "交易所问询", packs=PACK, check=_r_rg03),
        Rule("RG04", "上市地位风险", Dimension.REGULATORY,
             "退市或停牌", packs=PACK, check=_r_rg04),
        Rule("RG05", "资产冻结", Dimension.REGULATORY,
             "资产受限", packs=PACK, check=_r_rg05),
        Rule("OP01", "盈利警告或业绩预告", Dimension.OPERATION,
             "经营预警", packs=PACK, check=_r_op01),
        Rule("OP02", "审计意见类型", Dimension.OPERATION,
             "是否非标意见", packs=PACK, check=_r_op02),
        Rule("OP03", "资本开支强度", Dimension.OPERATION,
             "投资强度", requires=["metric:capex", "metric:total_assets"], packs=PACK, exclude_packs=FINANCIAL_PACKS, check=_r_op03),
        Rule("OP04", "商誉减值风险", Dimension.OPERATION,
             "商誉占比", packs=PACK, check=_r_op04),
        Rule("OP05", "持续经营能力", Dimension.OPERATION,
             "现金流与盈利双重信号", requires=["metric:ocf", "metric:net_profit"],
             packs=PACK, check=_r_op05),
    ]
