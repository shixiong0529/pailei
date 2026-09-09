"""东方财富数据中心适配器：A 股与港股财务报表。

已实测可用的接口（2026-09-05 验证，均无需账号鉴权）：
- A 股：RPT_DMSK_FN_BALANCE / RPT_DMSK_FN_INCOME / RPT_DMSK_FN_CASHFLOW / RPT_F10_QTR_MAINFINADATA
- A 股资料：RPT_F10_ORG_BASICINFO
- 港股：RPT_HKF10_FN_BALANCE_PC / _INCOME_PC / _CASHFLOW_PC（长表）/ RPT_HKF10_FN_MAININDICATOR
- 港股资料：RPT_HKF10_INFO_ORGPROFILE
- 搜索：searchapi.eastmoney.com suggest

金额单位：A 股为人民币元；港股 AMOUNT 为报表币种元（币种由 MAININDICATOR.CURRENCY 给出）。
"""

from __future__ import annotations

import re
import math
from typing import Any, Iterable

from app.config import settings
from app.core.http_client import FetchError, HttpClient
from app.core.models import (
    FinancialFact,
    Market,
    PeriodType,
    Statement,
    now_iso,
)

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


# ---------------------------------------------------------------- 标准科目映射

A_BALANCE_MAP: dict[str, str] = {
    "TOTAL_ASSETS": "total_assets",
    "TOTAL_LIABILITIES": "total_liabilities",
    "TOTAL_EQUITY": "total_equity",
    "MONETARYFUNDS": "cash",
    "ACCOUNTS_RECE": "accounts_receivable",
    "INVENTORY": "inventory",
    "SHORT_LOAN": "short_term_borrowings",
    "LONG_LOAN": "long_term_borrowings",
    "CONTRACT_LIAB": "contract_liabilities",
    "TOTAL_CURRENT_ASSETS": "total_current_assets",
    "TOTAL_CURRENT_LIAB": "total_current_liabilities",
    "ACCOUNTS_PAYABLE": "accounts_payable",
    "FIXED_ASSET": "fixed_assets",
    "ADVANCE_RECEIVABLES": "advance_receivables",
    "PREPAYMENT": "prepayments",
    "GOODWILL": "goodwill",
    "NONCURRENT_LIAB_1YEAR": "current_noncurrent_liabilities",
    "BOND_PAYABLE": "bonds_payable",
    "LEASE_LIAB": "lease_liabilities",
    "CONTRACT_ASSET": "contract_assets",
    "CIP": "construction_in_progress",
    # 银行 / 券商 / 保险专用
    "CASH_DEPOSIT_PBC": "bank_cash_deposit_pbc",
    "LOAN_ADVANCE": "bank_loan_advance",
    "ACCEPT_DEPOSIT": "bank_accept_deposit",
    "BORROW_FUND": "bank_borrow_fund",
    "SELL_REPO_FINASSET": "broker_sell_repo_finasset",
    "AGENT_TRADE_SECURITY": "broker_agent_trade_security",
    "SETTLE_EXCESS_RESERVE": "broker_settle_excess_reserve",
    "PREMIUM_RECE": "ins_premium_receivable",
}

A_INCOME_MAP: dict[str, str] = {
    "TOTAL_OPERATE_INCOME": "total_revenue",
    "OPERATE_INCOME": "operating_revenue",
    "OPERATE_COST": "operating_cost",
    "TOTAL_OPERATE_COST": "total_operating_cost",
    "OPERATE_PROFIT": "operating_profit",
    "TOTAL_PROFIT": "pretax_profit",
    "PARENT_NETPROFIT": "net_profit_attributable",
    "DEDUCT_PARENT_NETPROFIT": "net_profit_deducted",
    "INCOME_TAX": "income_tax",
    "FINANCE_EXPENSE": "finance_expense",
    "MANAGE_EXPENSE": "admin_expense",
    "SALE_EXPENSE": "selling_expense",
    "OPERATE_EXPENSE": "rd_expense_proxy",
    "INVEST_INCOME": "investment_income",
    "OPERATE_TAX_ADD": "taxes_surcharges",
    # 银行 / 券商 / 保险专用
    "INTEREST_NI": "bank_net_interest_income",
    "FEE_COMMISSION_NI": "bank_net_fee_income",
    "EARNED_PREMIUM": "ins_earned_premium",
    "COMPENSATE_EXPENSE": "ins_claims_expense",
}

A_CASHFLOW_MAP: dict[str, str] = {
    "NETCASH_OPERATE": "ocf",
    "NETCASH_INVEST": "icf",
    "NETCASH_FINANCE": "fcf",
    "SALES_SERVICES": "cash_from_sales",
    "CONSTRUCT_LONG_ASSET": "capex",
    "END_CCE": "end_cash",
    "BEGIN_CCE": "begin_cash",
    "CCE_ADD": "cash_net_change",
    "PAY_STAFF_CASH": "cash_paid_to_staff",
    "INVEST_PAY_CASH": "cash_paid_investment",
    "RECEIVE_INTEREST_COMMISSION": "interest_received",
}

# 港股：按 STD_ITEM_CODE 映射（代码在不同报表间保持一致语义）
HK_BALANCE_MAP: dict[str, str] = {
    "004009999": "total_assets",
    "004025999": "total_liabilities",
    "004036999": "total_equity",
    "004028999": "net_assets",
    "004002999": "total_current_assets",
    "004011999": "total_current_liabilities",
    "004002010": "cash_equivalents",
    "004002009": "restricted_cash",
    "004002011": "short_term_deposits",
    "004002003": "accounts_receivable",
    "004002001": "inventory",
    "004002005": "prepayments_other_receivables",
    "004011001": "accounts_payable",
    "004011010": "short_term_borrowings",
    "004020001": "long_term_borrowings",
    "004027999": "minority_interest",
    "004001002": "fixed_assets",
    "004001004": "intangible_assets",
    "004013999": "net_current_assets",
    "004015999": "total_assets_less_current_liab",
}

HK_INCOME_MAP: dict[str, str] = {
    "004001001": "revenue",
    "004001999": "total_revenue",
    "004007999": "gross_profit",
    "004010999": "operating_profit",
    "004011999": "pretax_profit",
    "004012999": "net_profit",
    "004025002": "net_profit_attributable",
    "004012001": "income_tax",
    "004011201": "finance_cost",
    "004011200": "interest_income",
    "004005001": "operating_expenses",
    "004010004": "admin_expense",
    "004010003": "selling_expense",
    "004099999": "non_operating_items",
}

HK_CASHFLOW_MAP: dict[str, str] = {
    "003999": "ocf",
    "005999": "icf",
    "007999": "fcf",
    "011999": "end_cash",
    "011001": "begin_cash",
    "010999": "cash_net_change",
    "005011": "cash_paid_investment",
    "005007": "capex",
    "007003": "interest_paid",
    "007004": "dividend_paid",
    "003003": "tax_paid",
    "007002": "debt_repayment",
}

A_INDICATOR_KEEP = {
    "EPSJB": "eps_basic",
    "BPS": "bps",
    "PER_NETCASH": "operating_cashflow_per_share",
    "TOTALOPERATEREVE": "total_operating_revenue",
    "WEIGHTAVG_ROE": "roe_weighted",
    "ROEAVG_CUT": "roe_cut",
    "XSMLL": "gross_margin",
    "XSJLL": "net_margin",
    "ZCFZL": "debt_ratio",
    "MGWFPLR": "undistributed_profit_per_share",
}


def _period_type(code: str) -> PeriodType:
    try:
        return PeriodType(code)
    except ValueError:
        return PeriodType.ANNUAL


def _clean_date(value: str | None) -> str:
    if not value:
        return ""
    return str(value)[:10]


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "null", "None"}:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


class EastmoneyClient:
    """东方财富财务数据客户端。"""

    SOURCE_ID = "eastmoney"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()
        self._owns = client is None

    def close(self) -> None:
        if self._owns:
            self.client.close()

    def __enter__(self) -> EastmoneyClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- 基础查询

    def query_all(
        self,
        report_name: str,
        secucode: str,
        *,
        stage: str,
        page_size: int = 200,
        max_pages: int = 12,
        sort: str | None = "REPORT_DATE",
        desc: bool = True,
    ) -> list[dict[str, Any]]:
        """分页查询。sort=None 时不带排序参数（部分资料类报表不支持按 REPORT_DATE 排序）。"""
        rows: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params = {
                "reportName": report_name,
                "columns": "ALL",
                "filter": f'(SECUCODE="{secucode}")',
                "pageNumber": page,
                "pageSize": page_size,
                "source": "WEB",
                "client": "WEB",
            }
            if sort:
                params["sortColumns"] = sort
                params["sortTypes"] = -1 if desc else 1
            data = self.client.get_json(BASE, params=params, stage=stage)
            from app.data.provenance import preserve_response
            try:
                raw_ref = preserve_response(data, url=BASE, params=params)
            except (OSError, ValueError, TypeError) as exc:
                raise FetchError("无法保存原始财务响应，停止使用不可追溯的数据", url=BASE, stage=stage) from exc
            result = (data or {}).get("result") or {}
            chunk = result.get("data") or []
            rows.extend({**row, "_raw_ref": raw_ref} for row in chunk)
            pages = result.get("pages") or 1
            if page >= pages or not chunk:
                break
        return rows

    # -------------------------------------------------------------- 资料查询

    def a_profile(self, code: str) -> dict[str, Any]:
        rows = self.query_all("RPT_F10_ORG_BASICINFO", f"{code}.SH", stage="profile", max_pages=1, sort=None)
        if not rows:
            rows = self.query_all("RPT_F10_ORG_BASICINFO", f"{code}.SZ", stage="profile", max_pages=1, sort=None)
        if not rows:
            rows = self.query_all("RPT_F10_ORG_BASICINFO", f"{code}.BJ", stage="profile", max_pages=1, sort=None)
        return rows[0] if rows else {}

    def hk_profile(self, code: str) -> dict[str, Any]:
        rows = self.query_all("RPT_HKF10_INFO_ORGPROFILE", f"{code}.HK", stage="profile", max_pages=1, sort=None)
        return rows[0] if rows else {}

    def resolve_suffix(self, code: str) -> str | None:
        """识别 A 股代码应使用的后缀。返回 None 表示未命中。"""
        for suffix in ("SH", "SZ", "BJ"):
            try:
                rows = self.query_all(
                    "RPT_F10_ORG_BASICINFO", f"{code}.{suffix}", stage="resolve",
                    max_pages=1, page_size=1, sort=None,
                )
            except FetchError:
                continue
            if rows:
                return suffix
        return None

    # -------------------------------------------------------------- 财报获取

    def a_statements(self, secucode: str, years: int | None = None) -> list[FinancialFact]:
        years = years or settings.fiscal_years_back
        facts: list[FinancialFact] = []
        specs = [
            ("RPT_DMSK_FN_BALANCE", Statement.BALANCE, A_BALANCE_MAP),
            ("RPT_DMSK_FN_INCOME", Statement.INCOME, A_INCOME_MAP),
            ("RPT_DMSK_FN_CASHFLOW", Statement.CASHFLOW, A_CASHFLOW_MAP),
        ]
        for report, statement, mapping in specs:
            try:
                rows = self.query_all(report, secucode, stage=f"finance:{statement.value}")
            except FetchError as exc:
                facts.append(_error_fact(secucode, statement, str(exc)))
                continue
            for row in rows:
                facts.extend(_a_rows_to_facts(row, statement, mapping, report))
        facts.extend(self._a_indicators(secucode))
        return _limit_years(facts, years)

    def _a_indicators(self, secucode: str) -> list[FinancialFact]:
        try:
            rows = self.query_all(
                "RPT_F10_QTR_MAINFINADATA", secucode, stage="finance:indicator", max_pages=2
            )
        except FetchError:
            return []
        facts: list[FinancialFact] = []
        for row in rows:
            period = _clean_date(row.get("REPORT_DATE"))
            ptype = _a_period_type(row)
            for src, std in A_INDICATOR_KEEP.items():
                if src not in row:
                    continue
                value = _num(row.get(src))
                if value is None:
                    continue
                percent_items = {"roe_weighted", "roe_cut", "gross_margin", "net_margin", "debt_ratio",
                                 "roe_avg", "roa", "current_ratio", "bank_loan_deposit_ratio"}
                if std in percent_items:
                    value /= 100
                facts.append(
                    FinancialFact(
                        secucode=secucode,
                        statement=Statement.INDICATOR,
                        raw_ref=str(row.get("_raw_ref") or ""),
                raw_item=src,
                        std_item=std,
                        value=value,
                        unit="比率" if std in percent_items else "元",
                        currency="CNY",
                        period_end=period,
                        period_start=_period_start(period, ptype),
                        period_type=ptype,
                        fiscal_year=period[:4],
                        notice_date=_clean_date(row.get("NOTICE_DATE")),
                        audited=ptype is PeriodType.ANNUAL,
                        source_id=self.SOURCE_ID,
                        source_url=BASE,
                        fetched_at=now_iso(),
                        extraction="api",
                    )
                )
        return facts

    def hk_statements(self, secucode: str, years: int | None = None) -> list[FinancialFact]:
        years = years or settings.fiscal_years_back
        facts: list[FinancialFact] = []
        currency = self.hk_currency(secucode)
        specs = [
            ("RPT_HKF10_FN_BALANCE_PC", Statement.BALANCE, HK_BALANCE_MAP),
            ("RPT_HKF10_FN_INCOME_PC", Statement.INCOME, HK_INCOME_MAP),
            ("RPT_HKF10_FN_CASHFLOW_PC", Statement.CASHFLOW, HK_CASHFLOW_MAP),
        ]
        for report, statement, mapping in specs:
            try:
                rows = self.query_all(report, secucode, stage=f"finance:{statement.value}", page_size=500)
            except FetchError as exc:
                facts.append(_error_fact(secucode, statement, str(exc)))
                continue
            facts.extend(_hk_rows_to_facts(rows, secucode, statement, mapping, report, currency))
        facts.extend(self._hk_indicators(secucode, currency))
        return _limit_years(facts, years)

    def hk_currency(self, secucode: str) -> str:
        # MAININDICATOR.CURRENCY 可能是证券交易币种。必须由原始财报校验，不能直接标成港元。
        return "未核实"

    def _hk_indicators(self, secucode: str, currency: str) -> list[FinancialFact]:
        try:
            rows = self.query_all(
                "RPT_HKF10_FN_MAININDICATOR", secucode, stage="finance:indicator", max_pages=2
            )
        except FetchError:
            return []
        keep = {
            "BASIC_EPS": "eps_basic",
            "BPS": "bps",
            "ROE_AVG": "roe_avg",
            "ROA": "roa",
            "DEBT_ASSET_RATIO": "debt_ratio",
            "GROSS_PROFIT_RATIO": "gross_margin",
            "NET_PROFIT_RATIO": "net_margin",
            "CURRENT_RATIO": "current_ratio",
            "OPERATE_INCOME": "operating_revenue",
            "NETCASH_OPERATE": "ocf",
            "PER_NETCASH_OPERATE": "ocf_per_share",
            "ACCOUNTS_RECE_TDAYS": "ar_days",
            "INVENTORY_TDAYS": "inventory_days",
            "PREMIUM_INCOME": "ins_premium_income",
            "NET_INTEREST_INCOME": "bank_net_interest_income",
            "FEE_COMMISSION_INCOME": "bank_fee_income",
            "LOAN_DEPOSIT": "bank_loan_deposit_ratio",
        }
        facts: list[FinancialFact] = []
        for row in rows:
            period = _clean_date(row.get("REPORT_DATE") or row.get("STD_REPORT_DATE"))
            ptype = _period_type(str(row.get("DATE_TYPE_CODE") or "001"))
            cur = currency or "未核实"
            for src, std in keep.items():
                if src not in row:
                    continue
                value = _num(row.get(src))
                if value is None:
                    continue
                percent_items = {"roe_weighted", "roe_cut", "gross_margin", "net_margin", "debt_ratio",
                                 "roe_avg", "roa", "current_ratio", "bank_loan_deposit_ratio"}
                if std in percent_items:
                    value /= 100
                facts.append(
                    FinancialFact(
                        secucode=secucode,
                        statement=Statement.INDICATOR,
                        raw_ref=str(row.get("_raw_ref") or ""),
                raw_item=src,
                        std_item=std,
                        value=value,
                        unit=("天" if std in {"ar_days", "inventory_days"} else "倍" if std == "current_ratio"
                              else "比率" if std in percent_items else "元"),
                        currency=cur,
                        period_end=period,
                        period_start=_period_start(period, ptype),
                        period_type=ptype,
                        fiscal_year=period[:4],
                        notice_date=_clean_date(row.get("NOTICE_DATE")),
                        audited=ptype is PeriodType.ANNUAL,
                        source_id=self.SOURCE_ID,
                        source_url=BASE,
                        fetched_at=now_iso(),
                        extraction="api",
                        note="港股会计准则与财年可能与A股不同，口径已标注",
                    )
                )
        return facts

    # -------------------------------------------------------------- 证券搜索

    def search(self, keyword: str, count: int = 12) -> list[dict[str, Any]]:
        params = {
            "input": keyword,
            "type": "14",
            "token": SEARCH_TOKEN,
            "count": count,
        }
        try:
            data = self.client.get_json(SEARCH, params=params, stage="search")
        except FetchError:
            return []
        table = (data or {}).get("QuotationCodeTable") or {}
        return table.get("Data") or []


# ------------------------------------------------------------------ 内部函数


def _error_fact(secucode: str, statement: Statement, message: str) -> FinancialFact:
    return FinancialFact(
        secucode=secucode,
        statement=statement,
        raw_item="__error__",
        std_item="__error__",
        value=None,
        period_end="",
        source_id=EastmoneyClient.SOURCE_ID,
        fetched_at=now_iso(),
        verified=False,
        note=f"获取失败：{message}"[:300],
    )


def _a_period_type(row: dict[str, Any]) -> PeriodType:
    # A 股按自然财年披露；主要指标接口缺 DATE_TYPE_CODE 时不能一律当年报。
    end = _clean_date(row.get("REPORT_DATE"))
    return {"03-31": PeriodType.Q1, "06-30": PeriodType.INTERIM,
            "09-30": PeriodType.Q3, "12-31": PeriodType.ANNUAL}.get(
                end[5:], _period_type(str(row.get("DATE_TYPE_CODE") or "001")))


def _a_rows_to_facts(
    row: dict[str, Any], statement: Statement, mapping: dict[str, str], report: str
) -> list[FinancialFact]:
    period = _clean_date(row.get("REPORT_DATE"))
    ptype = _a_period_type(row)
    notice = _clean_date(row.get("NOTICE_DATE"))
    facts: list[FinancialFact] = []
    for src, std in mapping.items():
        if src not in row:
            continue
        value = _num(row.get(src))
        if value is None:
            continue
        facts.append(
            FinancialFact(
                secucode=str(row.get("SECUCODE") or ""),
                statement=statement,
                raw_ref=str(row.get("_raw_ref") or ""),
                raw_item=src,
                std_item=std,
                value=value,
                unit="元",
                currency="CNY",
                period_end=period,
                period_start=_period_start(period, ptype),
                period_type=ptype,
                fiscal_year=period[:4],
                notice_date=notice,
                audited=ptype is PeriodType.ANNUAL,
                consolidated=True,
                source_id=EastmoneyClient.SOURCE_ID,
                source_url=f"{BASE}?reportName={report}",
                fetched_at=now_iso(),
                extraction="api",
            )
        )
    return facts


def _hk_rows_to_facts(
    rows: Iterable[dict[str, Any]],
    secucode: str,
    statement: Statement,
    mapping: dict[str, str],
    report: str,
    currency: str,
) -> list[FinancialFact]:
    facts: list[FinancialFact] = []
    for row in rows:
        code = str(row.get("STD_ITEM_CODE") or "")
        std = mapping.get(code)
        if not std:
            continue
        period = _clean_date(row.get("REPORT_DATE"))
        ptype = _period_type(str(row.get("DATE_TYPE_CODE") or "001"))
        value = _num(row.get("AMOUNT"))
        raw_name = str(row.get("STD_ITEM_NAME") or "")
        facts.append(
            FinancialFact(
                secucode=secucode,
                statement=statement,
                raw_ref=str(row.get("_raw_ref") or ""),
                raw_item=raw_name,
                std_item=std,
                value=value,
                unit="元",
                currency=currency,
                period_end=period,
                period_start=_clean_date(row.get("START_DATE")) or _period_start(period, ptype),
                period_type=ptype,
                fiscal_year=period[:4],
                notice_date="",
                audited=ptype is PeriodType.ANNUAL,
                consolidated=True,
                source_id=EastmoneyClient.SOURCE_ID,
                source_url=f"{BASE}?reportName={report}",
                fetched_at=now_iso(),
                extraction="api",
                note=f"港股科目 {code} {raw_name}",
            )
        )
    return facts


def _period_start(period_end: str, ptype: PeriodType) -> str:
    """按报告期长度反推财年起始日，支持非自然财年；balance 为时点另作标注。"""
    from datetime import date, timedelta
    if not period_end:
        return ""
    months = {PeriodType.ANNUAL: 12, PeriodType.INTERIM: 6, PeriodType.Q1: 3, PeriodType.Q3: 9}[ptype]
    try:
        end = date.fromisoformat(period_end)
        first_next = end + timedelta(days=1)
        absolute = first_next.year * 12 + first_next.month - 1 - months
        return date(absolute // 12, absolute % 12 + 1, 1).isoformat()
    except ValueError:
        return ""


def _limit_years(facts: list[FinancialFact], years: int) -> list[FinancialFact]:
    if not facts:
        return facts
    annual_ends = sorted({f.period_end for f in facts if f.period_end and f.period_type is PeriodType.ANNUAL}, reverse=True)
    if annual_ends:
        oldest = annual_ends[:years][-1]
        # 保留最早完整财年的所有季度，外加最新已披露的非完整财年。
        cutoff = _period_start(oldest, PeriodType.ANNUAL)
        return [f for f in facts if not f.period_end or f.period_end >= cutoff]
    return facts


def market_of_secucode(secucode: str) -> Market:
    return Market.HK if secucode.upper().endswith(".HK") else Market.A


def normalize_code(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text or "").upper()
