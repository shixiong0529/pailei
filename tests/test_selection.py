"""V1.2 阶段 5 测试：披露文件选择与目标章节解析。

全离线、确定性。覆盖：
- 类别配额选择：重要类别不被例行公告挤出，同类例行公告设上限，总数不超上限；
- 目标章节定位与定向补充解析规划（长文档按上限截断时区分重点章节覆盖/未定位）；
- 解析缓存按 SHA256 + 解析器版本失效。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core.models import DisclosureDoc  # noqa: E402
from app.data.pdftext import (  # noqa: E402
    PDF_PARSER_VERSION,
    ParsedDoc,
    _cache_path,
    _load_cache,
    _store_cache,
    plan_target_parse,
    target_chapters_found,
)
from app.engine.selection import select_documents  # noqa: E402


def _doc(did, dtype, date):
    return DisclosureDoc(did, "000001.SZ", f"{dtype} {did}", date, "cninfo",
                         "https://example.org/a.pdf", doc_type=dtype)


class SelectionTests(unittest.TestCase):
    def test_important_categories_not_crowded_out(self):
        # 大量例行公告 + 少量重要处罚公告：处罚公告仍应被选中。
        docs = [_doc(f"routine{i}", "其他公告", f"2026-01-{i:02d}") for i in range(1, 29)]
        docs += [_doc("penalty1", "监管处罚", "2026-02-01"),
                 _doc("penalty2", "监管处罚", "2026-02-02"),
                 _doc("lawsuit1", "诉讼", "2026-02-03")]
        selected, _ = select_documents(docs, max_total=10)
        types = [d.doc_type for d in selected]
        self.assertIn("监管处罚", types)
        self.assertIn("诉讼", types)
        # 例行公告受上限约束，不会占满全部名额
        self.assertLess(types.count("其他公告"), 10)

    def test_routine_cap(self):
        docs = [_doc(f"r{i}", "其他公告", f"2026-01-{i:02d}") for i in range(1, 21)]
        selected, reasons = select_documents(docs, max_total=20, routine_cap=3)
        self.assertEqual(sum(1 for d in selected if d.doc_type == "其他公告"), 3)
        self.assertTrue(any("例行公告" in r for r in reasons))

    def test_total_not_exceeded(self):
        docs = [_doc(f"d{i}", "其他公告", f"2026-01-{i:02d}") for i in range(1, 50)]
        selected, _ = select_documents(docs, max_total=8)
        self.assertLessEqual(len(selected), 8)

    def test_quota_reason_recorded(self):
        docs = [_doc(f"p{i}", "监管处罚", f"2026-01-{i:02d}") for i in range(1, 15)]
        _, reasons = select_documents(docs, max_total=10)
        self.assertTrue(any("处罚与问询" in r and "配额" in r for r in reasons))


class TargetChapterTests(unittest.TestCase):
    def _parsed(self, pages, page_count, truncated):
        return ParsedDoc("doc", page_count, pages, truncated)

    def test_found_chapters_by_keyword(self):
        p = self._parsed([(1, "第一节 审计报告"), (2, "重大诉讼仲裁事项")], 2, False)
        found = target_chapters_found(p)
        self.assertIn("审计意见", found)
        self.assertIn("诉讼", found)
        self.assertNotIn("持续经营", found)

    def test_plan_target_parse_when_truncated_and_missing(self):
        p = self._parsed([(1, "审计报告")], 300, True)  # 只定位到审计意见
        rng = plan_target_parse(p, max_pages=120, extra_pages=60)
        self.assertEqual(rng, (120, 60))

    def test_plan_target_parse_none_when_complete(self):
        p = self._parsed([(1, "审计报告")], 100, False)
        self.assertIsNone(plan_target_parse(p, max_pages=120, extra_pages=60))

    def test_plan_target_parse_none_when_all_chapters_covered(self):
        pages = [(1, " ".join(kw)) for kw in
                 [("审计报告",), ("持续经营",), ("诉讼",), ("对外担保",),
                  ("受限资产",), ("关联交易",), ("短期借款",)]]
        p = self._parsed(pages, 300, True)
        self.assertIsNone(plan_target_parse(p, max_pages=120, extra_pages=60))


class ParseCacheTests(unittest.TestCase):
    def test_cache_key_includes_version(self):
        path = _cache_path("abc123", 120, 0)
        self.assertIn(PDF_PARSER_VERSION, path.name)
        self.assertIn("abc123", path.name)

    def test_cache_roundtrip_and_version_invalidation(self):
        tmp = tempfile.TemporaryDirectory(prefix="pailei-pcache-")
        try:
            with patch.object(settings, "cache_dir", Path(tmp.name)):
                parsed = ParsedDoc("doc", 5, [(1, "正文内容"), (2, "第二页")], False)
                _store_cache("sha123", 120, 0, parsed)
                self.assertEqual(_load_cache("sha123", 120, 0).full_text, "正文内容\n第二页")
                # 不同内容指纹不命中
                self.assertIsNone(_load_cache("other-sha", 120, 0))
                # 不同起始页不命中
                self.assertIsNone(_load_cache("sha123", 120, 1))
        finally:
            tmp.cleanup()

    def test_successful_truncated_range_is_cached(self):
        tmp = tempfile.TemporaryDirectory(prefix="pailei-pcache-partial-")
        try:
            with patch.object(settings, "cache_dir", Path(tmp.name)):
                parsed = ParsedDoc("doc", 300, [(i, f"第{i}页") for i in range(1, 121)], True)
                _store_cache("sha-partial", 120, 0, parsed)
                cached = _load_cache("sha-partial", 120, 0)
                self.assertIsNotNone(cached, "明确页段的成功解析即使截断也应复用")
                self.assertTrue(cached.truncated)
                self.assertEqual(len(cached.pages), 120)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
