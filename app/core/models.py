"""核心数据对象。

对应开发方案 §7：公司、证券、披露文件、财务事实、证据、风险事件、规则结果、扫描任务、报告版本。
每个对象都保留来源与口径信息，满足 §5 的财务口径与证据设计要求。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from enum import Enum
from typing import Any, Optional


class Market(str, Enum):
    A = "A"      # 沪市 / 深市 / 北交所
    HK = "HK"    # 香港交易所


class PeriodType(str, Enum):
    """报告期类型。对应东方财富 DATE_TYPE_CODE。"""

    ANNUAL = "001"     # 年报
    INTERIM = "002"    # 中报
    Q1 = "003"         # 一季报
    Q3 = "004"         # 三季报

    @property
    def label(self) -> str:
        return {
            "001": "年报",
            "002": "中报",
            "003": "一季报",
            "004": "三季报",
        }.get(self.value, "未知期次")

    @property
    def is_cumulative_year_end(self) -> bool:
        return self is PeriodType.ANNUAL


class Statement(str, Enum):
    BALANCE = "balance"
    INCOME = "income"
    CASHFLOW = "cashflow"
    INDICATOR = "indicator"


class Dimension(str, Enum):
    FINANCIAL_QUALITY = "财务质量"
    SOLVENCY = "偿债能力"
    GOVERNANCE = "公司治理"
    REGULATORY = "监管法律"
    OPERATION = "经营行业"


class RuleStatus(str, Enum):
    """规则输出只能是这五种结论之一（方案 §6）。"""

    RISK = "发现风险"
    WATCH = "需要关注"
    NORMAL = "已覆盖资料中未发现明显异常"
    INSUFFICIENT = "数据不足，无法判断"
    NOT_APPLICABLE = "不适用"

    @property
    def order(self) -> int:
        return {"发现风险": 0, "需要关注": 1, "数据不足，无法判断": 2, "已覆盖资料中未发现明显异常": 3, "不适用": 4}[self.value]


class Severity(str, Enum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"
    UNKNOWN = "未定"


class EvidenceStrength(str, Enum):
    CONFIRMED = "原始披露确认"
    PARTIAL = "部分证据支持"
    WEAK = "线索待核实"


@dataclass
class Security:
    """证券标识。"""

    code: str                      # 600519 / 00700
    market: Market
    name: str                      # 简称
    secucode: str                  # 600519.SH / 00700.HK
    org_code: str = ""             # 数据商内部主体代码
    org_name: str = ""             # 公司全称
    exchange: str = ""
    currency: str = "CNY"
    industry: str = ""
    listing_date: str = ""
    status: str = ""
    profile: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["market"] = self.market.value
        return d

    @property
    def auditor(self) -> str:
        return str(self.profile.get("auditor") or "")

    @property
    def former_names(self) -> str:
        return str(self.profile.get("former_names") or "")


@dataclass
class Company:
    """公司主体。A/H 双重上市时，多个 Security 指向同一 Company。"""

    company_id: str
    org_name: str
    securities: list[Security] = field(default_factory=list)
    industry: str = ""
    country: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "org_name": self.org_name,
            "industry": self.industry,
            "country": self.country,
            "securities": [s.to_dict() for s in self.securities],
        }


@dataclass
class FinancialFact:
    """单条财务事实。原值与标准化结果分开保存（方案 §5）。"""

    secucode: str
    statement: Statement
    raw_item: str                  # 原始科目名
    std_item: str                  # 标准科目代码
    value: Optional[float]
    unit: str = "元"
    currency: str = "CNY"
    period_end: str = ""           # 2026-06-30
    period_start: str = ""
    period_type: PeriodType = PeriodType.ANNUAL
    fiscal_year: str = ""
    notice_date: str = ""          # 公告日期
    audited: Optional[bool] = None  # 年报视为已审计，中报未审计（另有说明除外）
    consolidated: bool = True       # 合并口径
    source_id: str = ""
    source_url: str = ""
    fetched_at: str = ""
    extraction: str = "api"        # api / pdf / derived
    verified: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["statement"] = self.statement.value
        d["period_type"] = self.period_type.value
        return d


@dataclass
class DisclosureDoc:
    """披露文件（公告 / 财报 PDF）。"""

    doc_id: str
    secucode: str
    title: str
    publish_date: str
    source: str                    # cninfo / hkexnews / eastmoney
    url: str
    doc_type: str = ""             # 年报 / 半年报 / 公告 / 通函
    local_path: str = ""
    sha256: str = ""
    size_bytes: int = 0
    page_count: int = 0
    parsed: bool = False
    parse_error: str = ""
    text_excerpt: str = ""
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evidence:
    """证据片段。必须可被程序校验其引用的文档和片段真实存在（方案 §11）。"""

    evidence_id: str
    doc_id: str
    title: str
    quote: str
    location: str = ""             # 页码 / 章节 / 表格
    url: str = ""
    source: str = ""
    publish_date: str = ""
    fingerprint: str = ""          # 片段内容指纹
    verified: bool = False         # 是否通过原文存在性校验
    verify_note: str = ""

    @staticmethod
    def fingerprint_of(text: str) -> str:
        normalized = re.sub(r"\s+", "", text or "")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuleResult:
    """单条规则的检查结果。"""

    rule_id: str
    name: str
    dimension: Dimension
    status: RuleStatus
    severity: Severity = Severity.UNKNOWN
    strength: EvidenceStrength = EvidenceStrength.PARTIAL
    finding: str = ""              # 发现了什么
    why: str = ""                  # 为什么值得关注
    metric_snapshot: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    still_effective: Optional[bool] = None   # 当前是否仍存在
    mitigations: list[str] = field(default_factory=list)
    to_verify: list[str] = field(default_factory=list)
    industry_pack: str = "general"
    rule_version: str = "1.0"
    ai_interpreted: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dimension"] = self.dimension.value
        d["status"] = self.status.value
        d["severity"] = self.severity.value
        d["strength"] = self.strength.value
        return d


@dataclass
class RiskEvent:
    """风险事件，用于时间线展示与后续进展追踪。"""

    event_id: str
    title: str
    occurred_date: str
    category: str
    summary: str
    source_doc_id: str = ""
    resolved: Optional[bool] = None
    resolution_note: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 事件类型的固定枚举：模型生成的候选事件只能使用这些类型，其他类型一律拒收。
EVENT_CATEGORIES = [
    "监管处罚", "监管调查", "诉讼", "资产冻结", "监管问询", "上市地位",
    "财务更正", "盈利警告", "审计机构", "股权质押", "担保", "关联交易",
    "高管变动", "股东减持", "质押冻结",
]


class TaskStatus(str, Enum):
    QUEUED = "排队"
    RUNNING = "运行"
    PARTIAL = "部分完成"
    SUCCEEDED = "完成"
    FAILED = "失败"
    CANCELLED = "取消"


class Stage(str, Enum):
    IDENTIFY = "公司识别"
    PLAN = "检索规划"
    COLLECT = "资料获取"
    NORMALIZE = "标准化"
    RULE = "规则检查"
    READ = "专项阅读"
    VERIFY = "核验"
    REPORT = "报告生成"


STAGE_ORDER = [s for s in Stage]

STAGE_DETAIL = {
    Stage.IDENTIFY: "匹配证券、公司、行业及 A/H 关系",
    Stage.PLAN: "根据市场、行业、报告期生成资料清单",
    Stage.COLLECT: "分页收集公告、下载财报、去重",
    Stage.NORMALIZE: "提取科目、币种、单位、报告期并标准化",
    Stage.RULE: "程序计算指标并判断规则是否触发",
    Stage.READ: "AI 阅读相关附注、公告与事件进展",
    Stage.VERIFY: "检查主体、日期、来源与结论是否对应",
    Stage.REPORT: "结构化结果渲染为 HTML 报告",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_str() -> str:
    return date.today().isoformat()
