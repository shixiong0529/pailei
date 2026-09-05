"""数值型信号的定向原文定位。关键词命中只作为相关阅读，不自动确认计算结论。"""
from __future__ import annotations
import re
from app.core.models import Evidence
from app.data.pdftext import verify_evidence

# 科目别名用于匹配表格行，不用公司名称或任意年报首页兜底。
ALIASES = {
    "net_profit_attributable": ["归属于", "歸屬於", "權益持有人應佔盈利", "权益持有人应占盈利"],
    "net_profit": ["净利润", "淨利潤", "期內盈利", "期内盈利"],
    "revenue": ["收入", "收益"], "total_revenue": ["营业总收入", "收入", "收益"],
    "operating_revenue": ["营业收入", "收入", "收益"],
    "ocf": ["经营活动产生的现金流量净额", "經營業務現金淨額", "經營活動所得現金"],
    "accounts_receivable": ["应收账款", "應收賬款", "應收帳款"],
    "inventory": ["存货", "存貨"], "cash": ["货币资金"],
    "cash_equivalents": ["現金及現金等價物", "现金及现金等价物"],
    "restricted_cash": ["受限制现金", "受限制現金", "受限资金"],
    "total_assets": ["资产总计", "資產總額", "總資產", "总资产"],
    "total_liabilities": ["负债合计", "負債總額", "總負債", "总负债"],
    "short_term_borrowings": ["短期借款", "短期貸款", "流動借貸"],
    "long_term_borrowings": ["长期借款", "長期貸款", "非流動借貸"],
    "finance_expense": ["财务费用"], "finance_cost": ["融資成本", "財務費用"],
    "operating_profit": ["营业利润", "經營盈利"],
    "capex": ["购建固定资产", "購買物業", "資本開支"],
    "advance_receivables": ["预收款项"], "contract_liabilities": ["合同负债", "合約負債"],
}
GROUPS = {
    "FQ01": ["net_profit", "ocf"], "FQ02": ["ocf"], "FQ03": ["accounts_receivable", "revenue", "total_assets"],
    "FQ04": ["accounts_receivable", "revenue"], "FQ05": ["inventory", "revenue", "total_assets"],
    "FQ08": ["net_profit"], "FQ09": ["revenue"], "FQ10": ["net_profit"],
    "SV01": ["cash", "restricted_cash", "short_term_borrowings"],
    "SV02": ["total_assets", "total_liabilities"], "SV04": ["operating_profit", "finance_expense"],
    "SV05": ["short_term_borrowings", "long_term_borrowings", "total_assets"],
    "SV06": ["ocf", "capex"], "SV07": ["cash", "restricted_cash"],
    "OP03": ["capex", "total_assets"], "OP05": ["net_profit", "ocf"],
    "RE01": ["total_assets", "total_liabilities", "advance_receivables", "contract_liabilities"],
    "RE02": ["cash", "restricted_cash", "short_term_borrowings"],
}


def numeric_evidence(outcome, ctx) -> list[str]:
    periods = [ctx.metrics.latest_period]
    if outcome.rule.rule_id in {"FQ03", "FQ05", "FQ09", "FQ10"} and ctx.metrics.prior_period:
        periods.append(ctx.metrics.prior_period)
    ids = []
    for period in periods:
        for item in GROUPS.get(outcome.rule.rule_id, []):
            alternatives = {"net_profit": ["net_profit_attributable", "net_profit"],
                            "revenue": ["total_revenue", "operating_revenue", "revenue"],
                            "cash": ["cash", "cash_equivalents"],
                            "finance_expense": ["finance_expense", "finance_cost"]}.get(item, [item])
            fact = next((ctx.facts.get(key, period) for key in alternatives if ctx.facts.get(key, period)), None)
            if fact is None or fact.value is None or fact.currency in {"", "未核实"}:
                continue
            labels = ALIASES.get(fact.std_item, [])
            for doc in ctx.docs:
                parsed = ctx.parsed.get(doc.doc_id)
                if not parsed or parsed.error or doc.doc_type not in {"年报", "半年报", "季报", "业绩"}:
                    continue
                if doc.publish_date and doc.publish_date < period:
                    continue
                found_page = False
                for page, text in parsed.pages:
                    # 只接受科目所在行中真实出现的金额；保留整行与附近单位/期间上下文。
                    for line in text.splitlines():
                        if not any(label in re.sub(r"\s+", "", line) for label in labels):
                            continue
                        compact = line.replace(",", "").replace("，", "")
                        matched = False
                        for scale in (1, 1e3, 1e4, 1e6, 1e8):
                            amount = abs(fact.value) / scale
                            if amount < 1:
                                continue
                            for token in (f"{amount:.2f}", f"{amount:.0f}"):
                                if re.search(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", compact):
                                    matched = True
                        if not matched:
                            continue
                        offset = text.find(line)
                        quote = text[max(0, offset - 180):offset + len(line) + 180][:900]
                        ev = Evidence(f"{doc.doc_id}:p{page}:{Evidence.fingerprint_of(quote)}", doc.doc_id,
                                      doc.title, quote, location=f"第 {page} 页", url=doc.url,
                                      source=doc.source, publish_date=doc.publish_date,
                                      fingerprint=Evidence.fingerprint_of(quote))
                        verify_evidence(ev, parsed)
                        if ev.verified:
                            ids.append(ctx.evidence.add(ev, doc_type=doc.doc_type))
                            found_page = True
                        break
                    if found_page:
                        break
    return list(dict.fromkeys(ids))[:6]
