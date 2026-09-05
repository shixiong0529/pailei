"""指标计算：全部由程序完成，AI 不参与数值生成（方案 §4 / §6）。

边界处理（方案 §11 确定性测试样本要求）：
- 分母为零、缺失或接近零时不计算，返回 None；
- 负利润不套用普通同比解释；
- 累计值只与同口径累计值比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.models import Market, PeriodType, Statement
from app.engine.normalize import FactSet, growth, safe_div


@dataclass
class Metric:
    key: str
    label: str
    value: Optional[float]
    unit: str = "比率"
    basis: str = ""
    periods: list[str] = field(default_factory=list)
    formula: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "periods": self.periods,
            "formula": self.formula,
            "available": self.available,
        }


@dataclass
class MetricsBundle:
    metrics: dict[str, Metric] = field(default_factory=dict)
    latest_period: str = ""
    latest_period_type: Optional[PeriodType] = None
    prior_period: str = ""
    currency: str = "CNY"
    notes: list[str] = field(default_factory=list)

    def get(self, key: str) -> Optional[float]:
        m = self.metrics.get(key)
        return m.value if m else None

    def has(self, key: str) -> bool:
        m = self.metrics.get(key)
        return bool(m and m.value is not None)

    def add(self, m: Metric) -> None:
        self.metrics[m.key] = m

    def as_dict(self) -> dict[str, dict]:
        return {k: v.to_dict() for k, v in self.metrics.items()}

    def snapshot(self, keys: list[str]) -> dict[str, Optional[float]]:
        return {k: self.get(k) for k in keys}


# 标准科目 → 展示名
ITEM_LABELS = {
    "total_revenue": "营业总收入",
    "operating_revenue": "营业收入",
    "revenue": "营业额",
    "operating_cost": "营业成本",
    "gross_profit": "毛利",
    "operating_profit": "营业利润",
    "pretax_profit": "利润总额",
    "net_profit": "净利润",
    "net_profit_attributable": "归属母公司净利润",
    "net_profit_deducted": "扣非净利润",
    "income_tax": "所得税费用",
    "finance_expense": "财务费用",
    "finance_cost": "融资成本",
    "ocf": "经营活动现金流净额",
    "icf": "投资活动现金流净额",
    "fcf": "筹资活动现金流净额",
    "cash_from_sales": "销售商品收到现金",
    "capex": "购建长期资产支付现金",
    "end_cash": "期末现金",
    "cash": "货币资金",
    "restricted_cash": "受限资金",
    "accounts_receivable": "应收账款",
    "inventory": "存货",
    "total_assets": "总资产",
    "total_liabilities": "总负债",
    "total_equity": "所有者权益",
    "net_assets": "净资产",
    "short_term_borrowings": "短期借款",
    "long_term_borrowings": "长期借款",
    "accounts_payable": "应付账款",
    "total_current_assets": "流动资产合计",
    "total_current_liabilities": "流动负债合计",
    "minority_interest": "少数股东权益",
}


def compute_metrics(
    facts: FactSet,
    *,
    market: Market = Market.A,
    industry: str = "",
) -> MetricsBundle:
    """基于标准化财务事实计算指标。"""
    latest = facts.latest_period()
    ptype = facts.latest_period_type()
    if not latest or ptype is None:
        return MetricsBundle(notes=["未获取到任何财务报告期，所有指标无法计算"])

    # 同口径上一期
    same_type_periods = facts.periods(ptype)
    prior = same_type_periods[1] if len(same_type_periods) > 1 else ""
    currencies = facts.currencies()
    bundle = MetricsBundle(
        latest_period=latest,
        latest_period_type=ptype,
        prior_period=prior,
        currency=currencies[0] if currencies else "CNY",
    )
    basis = f"{latest[:4]}年{ptype.label}"
    prior_basis = f"{prior[:4]}年{ptype.label}" if prior else ""

    def put(key: str, label: str, value: Optional[float], unit="比率", formula="", periods=None):
        bundle.add(
            Metric(
                key=key, label=label, value=value, unit=unit,
                basis=basis if not periods else f"{basis} vs {prior_basis}",
                periods=periods or [latest], formula=formula,
            )
        )

    def cur(item: str) -> Optional[float]:
        return facts.value(item, latest)

    def prev(item: str) -> Optional[float]:
        return facts.value(item, prior) if prior else None

    # ---------------- 规模与盈利 ----------------
    revenue_items = ["total_revenue", "operating_revenue", "revenue"]
    rev_key = next((k for k in revenue_items if cur(k) is not None), "")
    revenue = cur(rev_key) if rev_key else None
    put("revenue", ITEM_LABELS.get(rev_key, "营业收入"), revenue, unit="元",
        formula=f"{ITEM_LABELS.get(rev_key, '营业收入')}({latest})")

    profit_key = next(
        (k for k in ["net_profit_attributable", "net_profit"] if cur(k) is not None), ""
    )
    net_profit = cur(profit_key) if profit_key else None
    put("net_profit", ITEM_LABELS.get(profit_key, "净利润"), net_profit, unit="元",
        formula=f"{ITEM_LABELS.get(profit_key, '净利润')}({latest})")

    deducted = cur("net_profit_deducted")
    put("net_profit_deducted", "扣非净利润", deducted, unit="元", formula="扣非净利润")
    put(
        "nonrecurring_ratio", "非经常性损益占净利润比",
        safe_div(
            (net_profit - deducted) if (net_profit is not None and deducted is not None) else None,
            net_profit,
        ),
        formula="(净利润 - 扣非净利润) / 净利润",
    )

    gross = cur("gross_profit")
    put("gross_margin", "毛利率", safe_div(gross, revenue), formula="毛利 / 营业收入")
    put("net_margin", "净利率", safe_div(net_profit, revenue), formula="净利润 / 营业收入")

    # ---------------- 现金流 ----------------
    ocf = cur("ocf")
    put("ocf", "经营活动现金流净额", ocf, unit="元", formula="经营活动现金流净额")
    put("ocf_to_net_profit", "经营现金流 / 净利润", safe_div(ocf, net_profit),
        formula="经营活动现金流净额 / 净利润")
    put("ocf_to_revenue", "经营现金流 / 营业收入", safe_div(ocf, revenue),
        formula="经营活动现金流净额 / 营业收入")
    capex = cur("capex")
    put("capex", "购建长期资产支付现金", capex, unit="元", formula="购建长期资产支付现金")
    if ocf is not None and capex is not None:
        put("fcf_proxy", "经营现金流 - 资本开支", ocf - capex, unit="元",
            formula="经营活动现金流净额 - 购建长期资产现金")
    put("cash_from_sales_to_revenue", "销售收现 / 营业收入",
        safe_div(cur("cash_from_sales"), revenue), formula="销售商品收到现金 / 营业收入")

    # ---------------- 资产与偿债 ----------------
    assets = cur("total_assets")
    liabilities = cur("total_liabilities")
    equity = cur("total_equity") or cur("net_assets")
    put("total_assets", "总资产", assets, unit="元")
    put("total_liabilities", "总负债", liabilities, unit="元")
    put("total_equity", "所有者权益", equity, unit="元")
    put("debt_ratio", "资产负债率", safe_div(liabilities, assets), formula="总负债 / 总资产")

    cash = cur("cash")
    restricted = cur("restricted_cash")
    put("cash", "货币资金", cash, unit="元")
    put("restricted_cash", "受限资金", restricted, unit="元")
    st_debt = cur("short_term_borrowings")
    lt_debt = cur("long_term_borrowings")
    put("short_term_borrowings", "短期借款", st_debt, unit="元")
    put("long_term_borrowings", "长期借款", lt_debt, unit="元")
    total_debt = None if (st_debt is None and lt_debt is None) else (
        (st_debt or 0) + (lt_debt or 0)
    )
    put("total_interest_bearing_debt", "有息负债（短借+长借）", total_debt, unit="元")
    # 受限资金不计入可自由偿债现金（方案 §5）
    usable_cash = (cash or 0) - (restricted or 0) if cash is not None else None
    put("usable_cash", "可自由使用现金（扣除受限）", usable_cash, unit="元",
        formula="货币资金 - 受限存款及现金")
    put("cash_to_short_debt", "现金 / 短期借款", safe_div(usable_cash or cash, st_debt),
        formula="(货币资金 - 受限资金) / 短期借款")
    put("debt_to_assets_ex_cash", "有息负债 / 总资产", safe_div(total_debt, assets))

    cur_assets = cur("total_current_assets")
    cur_liab = cur("total_current_liabilities")
    put("current_ratio", "流动比率", safe_div(cur_assets, cur_liab), formula="流动资产 / 流动负债")
    if cur_assets is not None and cur_liab is not None and cur("inventory") is not None:
        put("quick_ratio", "速动比率",
            safe_div(cur_assets - (cur("inventory") or 0), cur_liab),
            formula="(流动资产 - 存货) / 流动负债")

    op_profit = cur("operating_profit")
    fin_exp = cur("finance_expense") or cur("finance_cost")
    put("interest_coverage", "利息保障倍数", safe_div(op_profit, fin_exp), unit="倍",
        formula="营业利润 / 财务费用")

    # ---------------- 营运效率 ----------------
    ar = cur("accounts_receivable")
    inventory = cur("inventory")
    put("accounts_receivable", "应收账款", ar, unit="元")
    put("inventory", "存货", inventory, unit="元")
    put("ar_to_revenue", "应收账款 / 营业收入", safe_div(ar, revenue))
    put("ar_to_assets", "应收账款 / 总资产", safe_div(ar, assets),
        formula="应收账款 / 总资产（用于重要性判断）")
    put("inventory_to_cost", "存货 / 营业成本", safe_div(inventory, cur("operating_cost")))
    put("inventory_to_assets", "存货 / 总资产", safe_div(inventory, assets),
        formula="存货 / 总资产（用于重要性判断）")

    # ---------------- 同比（严格同口径） ----------------
    if prior:
        rev_prev = prev(rev_key) if rev_key else None
        put("revenue_yoy", "营业收入同比", growth(revenue, rev_prev),
            formula=f"({basis} 营业收入 / {prior_basis} 营业收入) - 1", periods=[latest, prior])
        np_prev = prev(profit_key) if profit_key else None
        np_yoy = growth(net_profit, np_prev)
        put("net_profit_yoy", "净利润同比", np_yoy,
            formula=f"({basis} 净利润 / {prior_basis} 净利润) - 1", periods=[latest, prior])
        if np_yoy is None and net_profit is not None and np_prev is not None:
            bundle.notes.append(
                f"净利润同比未计算：{basis} 或 {prior_basis} 存在负利润，普通同比口径不适用"
            )
        ar_prev = prev("accounts_receivable")
        put("ar_yoy", "应收账款同比", growth(ar, ar_prev), periods=[latest, prior])
        inv_prev = prev("inventory")
        put("inventory_yoy", "存货同比", growth(inventory, inv_prev), periods=[latest, prior])
        # 应收/存货增速是否显著快于收入
        if bundle.get("ar_yoy") is not None and bundle.get("revenue_yoy") is not None:
            put("ar_growth_minus_revenue_growth", "应收账款增速 - 收入增速",
                bundle.get("ar_yoy") - bundle.get("revenue_yoy"), periods=[latest, prior])
        if bundle.get("inventory_yoy") is not None and bundle.get("revenue_yoy") is not None:
            put("inventory_growth_minus_revenue_growth", "存货增速 - 收入增速",
                bundle.get("inventory_yoy") - bundle.get("revenue_yoy"), periods=[latest, prior])
        ocf_prev = prev("ocf")
        put("ocf_yoy", "经营现金流同比", growth(ocf, ocf_prev), periods=[latest, prior])
    else:
        bundle.notes.append(
            f"缺少与 {basis} 同口径的上期数据，同比类指标无法计算"
        )

    # ---------------- 来自主要指标的补充 ----------------
    for key, label in (("roe_avg", "ROE"), ("roa", "ROA"), ("ar_days", "应收账款周转天数"),
                       ("inventory_days", "存货周转天数")):
        point = facts.latest(key)
        if point:
            unit = "天" if key.endswith("_days") else "比率"
            put(key, label, point.value, unit=unit, formula="数据服务商主要指标")

    # ---------------- 口径提示 ----------------
    bundle.notes.append(
        f"计算基准：{basis}（{'累计口径' if ptype in (PeriodType.ANNUAL, PeriodType.INTERIM, PeriodType.Q3) else '单季口径'}），"
        f"币种 {bundle.currency}"
    )
    if market is Market.HK:
        bundle.notes.append(
            "港股报表科目与会计准则可能与 A 股不同，本组指标按原报表口径计算，未做准则转换"
        )
    missing_core = [k for k in ("revenue", "net_profit", "ocf", "total_assets") if not bundle.has(k)]
    if missing_core:
        bundle.notes.append(
            "以下核心指标缺少原始数据，相关规则将无法判断："
            + "、".join(ITEM_LABELS.get(k, k) for k in missing_core)
        )
    return bundle
