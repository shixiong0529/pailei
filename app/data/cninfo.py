"""巨潮资讯网适配器：A 股公告检索与原文下载。

巨潮是深交所法定信息披露平台，同时提供沪市、深市、北交所公告全文。
- 检索：POST http://www.cninfo.com.cn/new/hisAnnouncement/query
- 主体查询：POST http://www.cninfo.com.cn/new/information/topSearch/query
- 原文：http://static.cninfo.com.cn/{adjunctUrl}

已实测（2026-09-05）：600519 在 2026-01-01~2026-09-05 区间返回 62 条公告，原文 PDF 可下载。
"""

from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.http_client import FetchError, HttpClient
from app.core.models import DisclosureDoc, now_iso
from app.core.storage import dedup

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
STATIC_BASE = "http://static.cninfo.com.cn/"

# column 参数：sse 上交所、szse 深交所；使用 szse 可覆盖两地（实际返回以 stock 参数为准）
COLUMN_MAP = {"SH": "sse", "SZ": "szse", "BJ": "bj"}


class CninfoClient:
    SOURCE_ID = "cninfo"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()
        self._owns = client is None

    def close(self) -> None:
        if self._owns:
            self.client.close()

    def __enter__(self) -> CninfoClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def search(self, keyword: str, max_num: int = 10) -> list[dict[str, Any]]:
        """按名称/代码查询公司主体，返回 orgId 等标识。"""
        try:
            data = self.client.request(
                "POST",
                SEARCH_URL,
                stage="cninfo:search",
                data={"keyWord": keyword, "maxNum": max_num},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": "http://www.cninfo.com.cn/new/index.jsp",
                },
            )
        except FetchError:
            return []
        try:
            return data.json() or []
        except Exception:
            return []

    def resolve_org(self, code: str) -> tuple[str, str] | None:
        """返回 (orgId, column)。

        巨潮 orgId 的构造规则不统一（实测 gssh0600519 / gssz0000001 / GD165627），
        因此优先用官方 topSearch 查询，失败时再退回构造。
        """
        hits = self.search(code)
        for hit in hits:
            if hit.get("code") == code:
                org_id = str(hit.get("orgId") or "")
                if not org_id:
                    continue
                if org_id.startswith("gssh"):
                    return org_id, "sse"
                if org_id.startswith("gssz"):
                    return org_id, "szse"
                return org_id, "szse"
        # 退回构造（仅限沪市，构造规则已验证；其余市场视为无法确认）
        if code.startswith(("60", "68")):
            return f"gssh0{code}", "sse"
        return None

    def announcements(
        self,
        code: str,
        org_id: str,
        start: date | None = None,
        end: date | None = None,
        max_items: int | None = None,
        column: str | None = None,
    ) -> dict[str, Any]:
        """分页拉取公告列表。返回公告列表与分页完成情况（缺口必须记录）。"""
        max_items = max_items or settings.max_announcements
        end = end or date.today()
        start = start or (end - timedelta(days=365 * max(1, settings.announcement_months // 12)))
        column = column or "sse"
        stock_param = f"{code},{org_id}"

        docs: list[DisclosureDoc] = []
        seen: set[str] = set()
        page = 1
        gaps: list[str] = []
        total = 0
        while len(docs) < max_items:
            payload = {
                "pageNum": page,
                "pageSize": 30,
                "column": column,
                "tabName": "fulltext",
                "plate": "",
                "stock": stock_param,
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start.isoformat()}~{end.isoformat()}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            try:
                resp = self.client.request(
                    "POST",
                    QUERY_URL,
                    stage="cninfo:announcements",
                    data=payload,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                    },
                )
                data = resp.json() or {}
            except (FetchError, Exception) as exc:
                gaps.append(f"第 {page} 页获取失败：{type(exc).__name__} {exc}"[:200])
                break

            total = data.get("totalRecordNum") or 0
            batch = data.get("announcements") or []
            if not batch:
                break
            for item in batch:
                ann_id = str(item.get("announcementId") or "")
                adjunct = item.get("adjunctUrl") or ""
                if not ann_id or ann_id in seen:
                    continue
                seen.add(ann_id)
                publish = _ms_to_date(item.get("announcementTime"))
                docs.append(
                    DisclosureDoc(
                        doc_id=f"cninfo:{ann_id}",
                        secucode=code,
                        title=(item.get("announcementTitle") or "").strip(),
                        publish_date=publish,
                        source=self.SOURCE_ID,
                        url=STATIC_BASE + adjunct if adjunct else "",
                        doc_type=_classify(item.get("announcementTitle") or ""),
                        size_bytes=int((item.get("adjunctSize") or 0) or 0) * 1024,
                        fetched_at=now_iso(),
                    )
                )
                if len(docs) >= max_items:
                    gaps.append(f"达到单次扫描公告上限 {max_items} 条，剩余未检查")
                    break
            if len(batch) < 30:
                break
            page += 1
            if page > 40:
                gaps.append("达到分页上限 40 页，剩余公告未检查")
                break
            time.sleep(0.2)

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
                doc.url, target, stage="cninfo:download", referer="http://www.cninfo.com.cn/"
            )
            doc.sha256 = _sha256(target)
            doc.local_path = str(dedup(target, doc.sha256))
            doc.size_bytes = target.stat().st_size
        except FetchError as exc:
            doc.parse_error = str(exc)[:300]
        return doc


def _ms_to_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000).date().isoformat()
    except Exception:
        return ""


def _classify(title: str) -> str:
    # 先判断事项，再判断载体（年度报告等），避免风险公告被归成普通财报。
    for key, label in [
        ("解除冻结", "冻结解除"), ("解除司法冻结", "冻结解除"),
        ("解除质押", "质押解除"), ("不减持", "减持承诺"),
        ("更正", "财务更正"), ("追溯调整", "财务更正"),
        ("问询函", "监管问询"), ("关注函", "监管问询"),
        ("会计师事务所", "审计机构"), ("业绩预告", "业绩预告"),
    ]:
        if key in title:
            return label
    rules = [
        # 必须先匹配更具体的表述，避免“半年度报告”被“年度报告”抢先命中
        ("半年度报告", "半年报"),
        ("第一季度报告", "一季报"),
        ("第三季度报告", "三季报"),
        ("年度报告", "年报"),
        ("季度报告", "季报"),
        ("审计报告", "审计"),
        ("问询函", "监管问询"),
        ("关注函", "监管问询"),
        ("处罚", "监管处罚"),
        ("立案", "监管调查"),
        ("诉讼", "诉讼"),
        ("仲裁", "诉讼"),
        ("冻结", "资产冻结"),
        ("质押", "股权质押"),
        ("减持", "股东减持"),
        ("增持", "股东增持"),
        ("担保", "担保"),
        ("关联交易", "关联交易"),
        ("辞职", "高管变动"),
        ("聘任", "高管变动"),
        ("会计师事务所", "审计机构"),
        ("退市", "上市地位"),
        ("更正", "财务更正"),
        ("追溯调整", "财务更正"),
    ]
    for key, label in rules:
        if key in title:
            return label
    return "其他公告"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
