"""证券身份识别：名称、简称、代码、曾用名匹配，以及 A/H 双重上市关联。

方案要求（§2 / §11）：
- 同名、模糊匹配及 A/H 双重上市必须展示候选项，由用户确认证券；
- 系统在后台关联共同的公司主体；
- 身份无法可靠确认时，停止扫描。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.models import Company, Market, Security
from app.data.eastmoney import EastmoneyClient, normalize_code

# 显式证券代码，如 600519.SH / 000002.SZ / 00700.HK / 830799.BJ
_SECUCODE_RE = re.compile(r"^\d{5,6}\.(SH|SZ|BJ|HK)$")

# 代码前缀 → (交易所, 板块)
A_PREFIX_RULES = [
    (("600", "601", "603", "605"), "上海证券交易所", "主板"),
    (("688", "689"), "上海证券交易所", "科创板"),
    (("000", "001", "002", "003"), "深圳证券交易所", "主板"),
    (("300", "301"), "深圳证券交易所", "创业板"),
    (("43", "83", "87", "88", "92", "8"), "北京证券交易所", "北交所"),
]


@dataclass
class Candidate:
    security: Security
    score: int
    match_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.security.to_dict(),
            "score": self.score,
            "match_reason": self.match_reason,
        }


@dataclass
class ResolveResult:
    ok: bool
    selected: Security | None = None
    candidates: list[Candidate] = field(default_factory=list)
    company: Company | None = None
    ambiguous: bool = False
    message: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "company": self.company.to_dict() if self.company else None,
            "ambiguous": self.ambiguous,
            "message": self.message,
            "notes": self.notes,
        }


def exchange_of(code: str, market: Market) -> tuple[str, str]:
    if market is Market.HK:
        return "香港交易所", "主板"
    for prefixes, exchange, board in A_PREFIX_RULES:
        if code.startswith(prefixes):
            return exchange, board
    return "未知交易所", "未知板块"


def detect_market(code: str) -> Market:
    """5 位纯数字且以 0 开头（如 00700）通常代表港股代码。"""
    normalized = normalize_code(code)
    if len(normalized) == 5 and normalized.isdigit() and normalized.startswith("0"):
        return Market.HK
    return Market.A


class IdentityResolver:
    """证券识别与主体关联。"""

    def __init__(self, client: EastmoneyClient | None = None):
        self.client = client or EastmoneyClient()
        self._owns = client is None

    def close(self) -> None:
        if self._owns:
            self.client.close()

    def __enter__(self) -> IdentityResolver:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 搜索

    def search(self, query: str, limit: int = 12) -> list[Candidate]:
        query = (query or "").strip()
        if not query:
            return []
        raw = self.client.search(query, count=limit)
        candidates: list[Candidate] = []
        code_q = normalize_code(query)
        for item in raw:
            code = str(item.get("Code") or "").strip()
            name = str(item.get("Name") or "").strip()
            market = _sec_market(item)
            if market is None or not code or not name:
                continue
            if market is Market.A and not code.isdigit():
                continue
            secucode = f"{code}.HK" if market is Market.HK else self._a_secucode(code)
            exchange, board = exchange_of(code, market)
            security = Security(
                code=code,
                market=market,
                name=name,
                secucode=secucode,
                exchange=exchange,
                currency="HKD" if market is Market.HK else "CNY",
                industry="",
                org_name=name,
            )
            score, reason = _score(query, code_q, code, name)
            candidates.append(Candidate(security, score, reason))
        candidates.sort(key=lambda c: (-c.score, c.security.market.value, c.security.code))
        return candidates[:limit]

    def _a_secucode(self, code: str) -> str:
        suffix = self.client.resolve_suffix(code)
        if suffix:
            return f"{code}.{suffix}"
        if code.startswith(("60", "68", "51", "58", "11")):
            return f"{code}.SH"
        if code.startswith(("00", "30", "12", "15", "16")):
            return f"{code}.SZ"
        return f"{code}.BJ"

    # ------------------------------------------------------------------ 识别

    def resolve(self, query: str) -> ResolveResult:
        query = (query or "").strip()
        # 显式证券代码（如 000002.SZ / 00700.HK）直接命中，不进入歧义流程
        if _SECUCODE_RE.match(query.upper()):
            return self._resolve_exact(query.upper())

        candidates = self.search(query)
        if not candidates:
            return ResolveResult(ok=False, message=f"未找到与“{query}”匹配的证券，扫描已停止")

        # 精确命中代码或名称时优先；否则返回候选让用户确认
        exact = [c for c in candidates if c.match_reason in ("代码精确匹配", "名称精确匹配")]
        notes: list[str] = []

        if len(exact) == 1:
            selected = exact[0]
        else:
            return ResolveResult(ok=False, candidates=candidates, ambiguous=True,
                                 message=f"“{query}”匹配到多个证券，请确认后重新扫描")

        security = selected.security
        enriched, enrich_notes = self.enrich(security)
        notes.extend(enrich_notes)

        company = Company(
            company_id=self._company_id(enriched),
            org_name=enriched.org_name or enriched.name,
            securities=[enriched],
            industry=enriched.industry,
        )

        # A/H 关联（启发式，标注待核实）
        pair, pair_note = self.find_ah_counterpart(enriched)
        if pair:
            company.securities.append(pair)
            notes.append(pair_note)

        return ResolveResult(
            ok=True,
            selected=enriched,
            candidates=candidates,
            company=company,
            ambiguous=len(candidates) > 1,
            message="" if len(candidates) == 1 else f"已按匹配度选择 {enriched.secucode}，另有 {len(candidates)-1} 个候选",
            notes=notes,
        )

    # ------------------------------------------------------------------ 补全

    def _resolve_exact(self, secucode: str) -> ResolveResult:
        """按显式证券代码解析（000002.SZ / 00700.HK）。"""
        candidates = self.search(secucode.split(".")[0])
        selected = next((c for c in candidates if c.security.secucode == secucode), None)
        if selected is None:
            return ResolveResult(ok=False, candidates=candidates,
                                 message=f"未能核实 {secucode} 的证券类别与交易所，扫描已停止")
        security = selected.security
        enriched, notes = self.enrich(security)
        if not enriched.org_name:
            return ResolveResult(
                ok=False, message=f"未在数据源中找到 {secucode} 对应的上市公司，扫描已停止"
            )
        company = Company(
            company_id=self._company_id(enriched),
            org_name=enriched.org_name,
            securities=[enriched],
            industry=enriched.industry,
        )
        pair, pair_note = self.find_ah_counterpart(enriched)
        if pair:
            company.securities.append(pair)
            notes.append(pair_note)
        return ResolveResult(
            ok=True, selected=enriched, candidates=[], company=company,
            ambiguous=False, message="按指定证券代码解析", notes=notes,
        )

    def enrich(self, security: Security) -> tuple[Security, list[str]]:
        """补全证券简称、公司全称、行业、币种及治理相关基础资料。"""
        notes: list[str] = []
        # 按代码构造的 Security（如 603986.SH 直达路径）没有简称，先回填
        if security.name == security.code:
            try:
                for item in self.client.search(security.code, count=5):
                    if str(item.get("Code") or "").strip() == security.code:
                        short = str(item.get("Name") or "").strip()
                        if short and short != security.code:
                            security.name = short
                        break
            except Exception:
                pass  # 简称获取失败不阻断扫描，标题回退用公司全称
        try:
            if security.market is Market.HK:
                profile = self.client.hk_profile(security.code)
                if profile.get("SECUCODE") and str(profile["SECUCODE"]).upper() != security.secucode:
                    raise ValueError("主数据证券市场不匹配")
                org_name = str(profile.get("ORG_NAME") or "")
                industry = str(
                    profile.get("BELONG_INDUSTRY") or profile.get("INDUSTRY_TYPE") or ""
                )
                currency = _currency_from(str(profile.get("CURRENCY") or ""), "HKD")
                listing = str(profile.get("LISTING_DATE") or "")[:10]
                security.profile = {
                    "auditor": str(profile.get("ACCOUNT_FIRM") or ""),
                    "chairman": str(profile.get("CHAIRMAN") or ""),
                    "org_type": str(profile.get("ORG_TYPE") or ""),
                    "fiscal_year_end": str(profile.get("YEAR_SETTLE_DAY") or ""),
                    "main_business": str(profile.get("MAIN_BUSINESS") or "")[:500],
                    "employees": profile.get("EMP_NUM"),
                    "register_place": str(profile.get("REG_PLACE") or ""),
                    "former_names": "",
                }
            else:
                profile = self.client.a_profile(security.code)
                if profile.get("SECUCODE") and str(profile["SECUCODE"]).upper() != security.secucode:
                    raise ValueError("主数据证券市场不匹配")
                org_name = str(profile.get("ORG_NAME") or "")
                industry = str(
                    profile.get("CSRC_INDUSTRY_NAME")
                    or profile.get("EM2016")
                    or profile.get("BOARD_NAME_1LEVEL")
                    or ""
                )
                currency = _currency_from(str(profile.get("CURRENCY") or ""), "CNY")
                listing = str(profile.get("LISTING_DATE") or "")[:10]
                security.profile = {
                    "auditor": str(profile.get("ACCOUNT_FIRM") or ""),
                    "chairman": str(profile.get("CHAIRMAN") or ""),
                    "legal_person": str(profile.get("LEGAL_PERSON") or ""),
                    "controller": str(profile.get("REAL_CONTROLER") or ""),
                    "org_type": str(profile.get("ORG_TYPE") or ""),
                    "former_names": str(profile.get("FORMERNAME") or ""),
                    "main_business": str(profile.get("MAIN_BUSINESS") or "")[:500],
                    "employees": profile.get("TOTAL_NUM"),
                    "register_place": str(profile.get("PROVINCE") or ""),
                    "listing_board": str(profile.get("TRADE_MARKET") or ""),
                }
            if org_name:
                security.org_name = org_name
            if industry:
                security.industry = industry
            if currency:
                security.currency = currency
            if listing:
                security.listing_date = listing
            if not org_name:
                notes.append(f"{security.secucode} 公司全称未能获取，报告中以证券简称代替")
            if not industry:
                notes.append(f"{security.secucode} 行业分类未获取，行业适配规则可能不适用")
        except Exception as exc:
            notes.append(f"{security.secucode} 公司概况获取异常：{type(exc).__name__}")
        return security, notes

    def find_ah_counterpart(self, security: Security) -> tuple[Security | None, str]:
        """按公司名称在另一市场寻找对应上市主体。

        这是启发式匹配：仅当另一市场存在同名证券时才建立关联，
        并在报告中标注为“待核实”，不作为事实断言。
        """
        target_market = Market.HK if security.market is Market.A else Market.A
        name_key = (security.org_name or security.name).replace("股份有限", "").replace("有限公司", "").strip()
        if not name_key:
            return None, ""
        try:
            raw = self.client.search(name_key, count=10)
        except Exception:
            return None, ""
        for item in raw:
            code = str(item.get("Code") or "").strip()
            name = str(item.get("Name") or "").strip()
            market = _sec_market(item)
            if market is None:
                continue
            if market is not target_market or not code:
                continue
            # A 股必须是 6 位数字代码；港股为 5 位数字。过滤掉非标准代码，避免误关联。
            if market is Market.A and not (code.isdigit() and len(code) == 6):
                continue
            if market is Market.HK and not (code.isdigit() and len(code) == 5):
                continue
            # 名称必须高度相似，避免同名不同主体的误报
            if _name_similarity(name_key, name) < 0.75:
                continue
            if code.lstrip("0") == security.code.lstrip("0"):
                continue
            exchange, _ = exchange_of(code, market)
            counterpart = Security(
                code=code,
                market=market,
                name=name,
                secucode=f"{code}.HK" if market is Market.HK else self._a_secucode(code),
                exchange=exchange,
                currency="HKD" if market is Market.HK else "CNY",
                industry=security.industry,
                org_name=name,
            )
            note = (
                f"检测到可能的 {'H' if market is Market.HK else 'A'} 股对应主体 {counterpart.secucode}"
                f"（{counterpart.name}），按名称启发式匹配，需人工核实"
            )
            return counterpart, note
        return None, ""

    def _company_id(self, security: Security) -> str:
        base = security.org_name or security.name
        return f"{security.market.value}:{base}"


def _sec_market(item: dict[str, Any]) -> Market | None:
    """从搜索条目识别市场；无法识别（指数/基金/债券等）返回 None。

    东方财富 suggest 接口的 Classify 字段对科创板返回交易所代码（如 "23"）
    而非 "AStock"，必须结合 SecurityTypeName（沪A/深A/京A/科创板/港股）判断。
    """
    classify = str(item.get("Classify") or "")
    sec_type = str(item.get("SecurityTypeName") or "")
    if classify == "HK" or sec_type == "港股":
        return Market.HK
    if classify in {"A", "AStock"} or sec_type in {"沪A", "深A", "京A", "科创板"}:
        return Market.A
    return None


def _score(query: str, code_q: str, code: str, name: str) -> tuple[int, str]:
    q = query.strip()
    if code_q and normalize_code(code) == code_q:
        return 100, "代码精确匹配"
    if q == name:
        return 95, "名称精确匹配"
    if q and q in name:
        return 80, "名称包含"
    if name and name in q:
        return 75, "查询词包含名称"
    if code_q and normalize_code(code).startswith(code_q):
        return 60, "代码前缀匹配"
    return 30, "模糊匹配"


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _currency_from(raw: str, default: str) -> str:
    raw = (raw or "").strip()
    mapping = {"港元": "HKD", "人民币": "CNY", "美元": "USD", "HKD": "HKD", "CNY": "CNY", "USD": "USD"}
    return mapping.get(raw, default)
