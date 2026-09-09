"""港交所披露易适配器：港股公告检索与原文下载。

- 主体查询：GET https://www1.hkexnews.hk/search/prefix.do （取内部 stockId）
- 公告检索：GET https://www1.hkexnews.hk/search/titleSearchServlet.do
- 原文：https://www1.hkexnews.hk{FILE_LINK}

已实测（2026-09-05）：00700 → stockId 7609；近 12 个月返回 177 条公告，PDF 可下载。
注意：这是公开网页接口，非 HKEX IIS 授权信息流。生产环境应评估接入条款。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.http_client import FetchError, HttpClient
from app.core.models import DisclosureDoc, now_iso
from app.core.storage import dedup

PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
BASE = "https://www1.hkexnews.hk"


class HkexnewsClient:
    SOURCE_ID = "hkexnews"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()
        self._owns = client is None

    def close(self) -> None:
        if self._owns:
            self.client.close()

    def __enter__(self) -> HkexnewsClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def resolve_stock_id(self, code: str) -> str | None:
        code5 = code.zfill(5)
        params = {
            "callback": "cb",
            "lang": "ZH",
            "type": "A",
            "name": code5,
            "market": "SEHK",
        }
        try:
            resp = self.client.request(
                "GET", PREFIX_URL, stage="hkex:resolve", params=params,
                headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml"},
            )
        except FetchError:
            return None
        match = re.search(r"\((\{.*\})\)", resp.text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        for item in data.get("stockInfo") or []:
            if str(item.get("code", "")).lstrip("0").zfill(5) == code5:
                return str(item.get("stockId"))
        return None

    def announcements(
        self,
        code: str,
        stock_id: str | None = None,
        start: date | None = None,
        end: date | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        max_items = max_items or settings.max_announcements
        end = end or date.today()
        start = start or (end - timedelta(days=30 * settings.announcement_months))
        stock_id = stock_id or self.resolve_stock_id(code)
        gaps: list[str] = []
        if not stock_id:
            return {
                "docs": [],
                "total": 0,
                "fetched": 0,
                "gaps": ["无法在披露易定位该股票编号（stockId 解析失败）"],
                "source": self.SOURCE_ID,
                "range": f"{start.isoformat()} ~ {end.isoformat()}",
            }

        # 披露易 JSON 接口的 page/startRow 参数实测无效（返回首屏同样内容）。
        # 改用时间窗口二分：当某窗口声明总数大于实际返回条数时，拆分区间重试。
        docs: list[DisclosureDoc] = []
        seen: set[str] = set()
        gaps: list[str] = []
        declared_total = 0
        windows: list[tuple[date, date]] = [(start, end)]
        budget = 24
        while windows and budget > 0:
            win_start, win_end = windows.pop(0)
            budget -= 1
            rows, total, err = self._query_window(stock_id, win_start, win_end)
            if err:
                gaps.append(f"{win_start}~{win_end} 获取失败：{err}")
                continue
            declared_total = max(declared_total, total)
            if total > len(rows) and (win_end - win_start).days >= 1:
                mid = win_start + (win_end - win_start) / 2
                mid_date = date.fromordinal(int(mid.toordinal()))
                if mid_date > win_start and mid_date < win_end:
                    windows.insert(0, (mid_date, win_end))
                    windows.insert(0, (win_start, mid_date))
                    continue
                if total > len(rows):
                    gaps.append(
                        f"{win_start}~{win_end} 声明 {total} 条，接口仅返回 {len(rows)} 条且无法再拆分"
                    )
            for row in rows:
                link = row.get("FILE_LINK") or ""
                news_id = str(row.get("NEWS_ID") or "")
                if not link or news_id in seen:
                    continue
                seen.add(news_id)
                docs.append(
                    DisclosureDoc(
                        doc_id=f"hkexnews:{news_id}",
                        secucode=code,
                        title=_clean_title(row.get("TITLE") or ""),
                        publish_date=_hk_date(row.get("DATE_TIME") or ""),
                        source=self.SOURCE_ID,
                        url=BASE + link if link.startswith("/") else link,
                        doc_type=_classify_hk(row.get("TITLE") or "", row.get("SHORT_TEXT") or ""),
                        fetched_at=now_iso(),
                    )
                )
                if len(docs) >= max_items:
                    gaps.append(f"达到单次扫描公告上限 {max_items} 条，剩余未检查")
                    break
            if len(docs) >= max_items:
                break
        if budget <= 0 and windows:
            gaps.append(f"达到公告检索请求预算（24 次），剩余 {len(windows)} 个时间窗口未检查")

        docs.sort(key=lambda d: d.publish_date, reverse=True)
        total = declared_total
        if total and len(docs) < total:
            gaps.append(f"接口声明共 {total} 条，实际获取 {len(docs)} 条")

        return {
            "docs": docs,
            "total": total,
            "fetched": len(docs),
            "gaps": gaps,
            "source": self.SOURCE_ID,
            "range": f"{start.isoformat()} ~ {end.isoformat()}",
        }

    def _query_window(self, stock_id: str, start: date, end: date) -> tuple[list[dict], int, str]:
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": start.strftime("%Y%m%d"),
            "toDate": end.strftime("%Y%m%d"),
            "title": "",
            "searchType": "1",
            "t1code": "-2",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": "100",
            "lang": "ZH",
        }
        try:
            resp = self.client.request(
                "GET", SEARCH_URL, stage="hkex:announcements", params=params,
                headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml"},
            )
            data = resp.json() or {}
        except (FetchError, Exception) as exc:
            return [], 0, f"{type(exc).__name__} {str(exc)[:150]}"
        try:
            rows = json.loads(data.get("result") or "[]")
        except json.JSONDecodeError:
            return [], 0, "列表解析失败"
        total = int(rows[0].get("TOTAL_COUNT") or 0) if rows else 0
        return rows, total, ""

    def download(self, doc: DisclosureDoc, files_dir: Path | None = None) -> DisclosureDoc:
        if not doc.url:
            doc.parse_error = "无原文链接"
            return doc
        files_dir = files_dir or settings.files_dir
        target = files_dir / doc.source / f"{doc.doc_id.replace(':', '_')}.pdf"
        if target.exists() and target.stat().st_size > 0:
            sha = _sha256(target)
            doc.sha256 = sha
            doc.local_path = str(dedup(target, sha))
            doc.size_bytes = target.stat().st_size
            return doc
        try:
            self.client.download(
                doc.url, target, stage="hkex:download",
                referer="https://www1.hkexnews.hk/search/titlesearch.xhtml",
            )
            doc.sha256 = _sha256(target)
            doc.local_path = str(dedup(target, doc.sha256))
            doc.size_bytes = target.stat().st_size
        except FetchError as exc:
            doc.parse_error = str(exc)[:300]
        return doc


def _clean_title(title: str) -> str:
    return re.sub(r"<[^>]+>", "", title).replace("＆#x2f;", "/").strip()


def _hk_date(raw: str) -> str:
    raw = (raw or "").strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _classify_hk(title: str, short_text: str) -> str:
    text = f"{title} {short_text}"
    if "證券變動月報表" in text or "证券变动月报表" in text:
        return "月报表"
    for key, label in [("盈利警告", "盈利警告"), ("溢利警告", "盈利警告"),
                       ("解除凍結", "冻结解除"), ("解除質押", "质押解除"),
                       ("更正", "财务更正"), ("追溯調整", "财务更正")]:
        if key in text:
            return label
    # 先识别例行文件：股东会、董事会会议等常规治理文件不构成风险信号
    routine_rules = [
        ("董事會會議召開日期", "董事会会议"),
        ("董事會會議", "董事会会议"),
        ("代表委任表格", "股东会文件"),
        ("股東週年大會", "股东会文件"),
        ("股东周年大会", "股东会文件"),
        ("重選董事", "股东会文件"),
        ("重选董事", "股东会文件"),
        ("發行及購回股份之一般授權", "一般授权"),
        ("一般性授權", "一般授权"),
        ("組織章程", "章程文件"),
        ("股份發行人的證券變動月報表", "月报表"),
        ("證券變動月報表", "月报表"),
    ]
    for key, label in routine_rules:
        if key in text:
            return label
    rules = [
        ("年報", "年报"),
        ("年度報告", "年报"),
        ("中期報告", "半年报"),
        ("中報", "半年报"),
        ("季度", "季报"),
        ("業績", "业绩"),
        ("核數師", "审计机构"),
        ("審計", "审计"),
        ("訴訟", "诉讼"),
        ("仲裁", "诉讼"),
        ("清盤", "上市地位"),
        ("除牌", "上市地位"),
        ("停牌", "上市地位"),
        ("調查", "监管调查"),
        ("處罰", "监管处罚"),
        ("關連交易", "关联交易"),
        ("關連", "关联交易"),
        ("質押", "股权质押"),
        ("股份購回", "股份回购"),
        ("配股", "再融资"),
        ("供股", "再融资"),
        ("董事", "高管变动"),
        ("辭任", "高管变动"),
        ("委任", "高管变动"),
        ("業績预告", "业绩预告"),
        ("盈利警告", "盈利警告"),
        ("盈利預警", "盈利警告"),
    ]
    for key, label in rules:
        if key in text:
            return label
    return "其他公告"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
