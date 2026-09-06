"""V1.2 阶段 7 测试：运行诊断与存储控制。

全离线、确定性。覆盖：
- 阶段耗时记录（开始/结束/墙钟耗时/摘要），八个阶段按顺序落库；
- 缓存命中率计数（模型 / PDF 解析），bump_stat 累计正确；
- 原始文件按内容指纹去重（同内容只保留一份物理副本）；
- 清理命令预览准确（PDF 解析缓存、模型缓存、被取代的旧报告版本），不删除报告文件/唯一原文。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import LLMConfig, settings  # noqa: E402
from app.core import db  # noqa: E402
from app.core.models import (  # noqa: E402
    FinancialFact,
    Market,
    PeriodType,
    Security,
    Stage,
    Statement,
    STAGE_ORDER,
)
from app.core.storage import dedup  # noqa: E402
from app.engine.metrics import compute_metrics  # noqa: E402
from app.engine.normalize import FactSet  # noqa: E402
from app.engine.rules.base import EvidenceStore, RuleContext  # noqa: E402
from app.engine.runner import build_registry, run_rules  # noqa: E402
from app.engine.pipeline import ScanPipeline  # noqa: E402
from app.llm.adapter import LLMAdapter  # noqa: E402


def _fact(std_item: str, period: str, value: float) -> FinancialFact:
    return FinancialFact(
        secucode="TEST.SZ", statement=Statement.INCOME, raw_item=std_item.upper(),
        std_item=std_item, value=value, period_end=period, period_type=PeriodType.INTERIM,
        fiscal_year=period[:4], currency="CNY",
    )


class StageTimingDBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pailei-diag-")
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(settings, "db_path", self.root / "app.db"),
            patch.object(settings, "reports_dir", self.root / "reports"),
            patch.object(settings, "files_dir", self.root / "files"),
            patch.object(settings, "cache_dir", self.root / "cache"),
        ]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_record_stage_and_order(self):
        db.record_stage("t1", "公司识别", 0, 100.0, 101.5, 1500, "识别为茅台")
        db.record_stage("t1", "检索规划", 1, 101.5, 102.0, 500, "规划完成")
        rows = db.stage_timings("t1")
        self.assertEqual([r["stage"] for r in rows], ["公司识别", "检索规划"])
        self.assertEqual(rows[0]["elapsed_ms"], 1500)
        self.assertEqual(rows[0]["summary"], "识别为茅台")
        self.assertTrue(rows[0]["started_at"])
        self.assertTrue(rows[0]["ended_at"])

    def test_stage_stats_aggregates(self):
        db.record_stage("t1", "公司识别", 0, 100.0, 102.0, 2000, "")
        db.record_stage("t2", "公司识别", 0, 100.0, 103.0, 3000, "")
        stats = {s["stage"]: s for s in db.stage_stats()}
        self.assertEqual(stats["公司识别"]["n"], 2)
        self.assertEqual(stats["公司识别"]["avg_ms"], 2500)
        self.assertEqual(stats["公司识别"]["max_ms"], 3000)

    def test_checkpoint_records_previous_stage(self):
        pipe = ScanPipeline("stage_direct", deadline_seconds=300)
        try:
            pipe._checkpoint(Stage.IDENTIFY)
            pipe._stage_summary[Stage.IDENTIFY.value] = "识别完成"
            pipe._checkpoint(Stage.PLAN)  # 触发记录 IDENTIFY
            rows = db.stage_timings("stage_direct")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stage"], "公司识别")
            self.assertEqual(rows[0]["summary"], "识别完成")
            self.assertGreaterEqual(rows[0]["elapsed_ms"], 0)
        finally:
            pipe.close()

    def test_full_pipeline_records_all_eight_stages(self):
        def _resolver():
            sec = Security("600519", Market.A, "贵州茅台", "600519.SH",
                           org_name="贵州茅台酒股份有限公司", industry="白酒")
            return SimpleNamespace(ok=True, selected=sec, company=None, notes=[])

        pipe = ScanPipeline("stage_full", deadline_seconds=300)
        pipe.llm = LLMAdapter(LLMConfig(api_key=""))
        pipe._collect_documents = lambda *a, **k: ([], {"source": "cninfo", "total": 0, "range": "x~y"})
        pipe._build_evidence = lambda *a, **k: (EvidenceStore(), {})
        vals = [_fact(k, "2026-06-30", v) for k, v in
                {"revenue": 100.0, "net_profit": 10.0, "ocf": 20.0, "total_assets": 200.0}.items()]
        pipe.em.a_statements = lambda *a, **k: vals
        clean = run_rules(
            RuleContext(
                Security("600519", Market.A, "贵州茅台", "600519.SH"),
                Market.A, FactSet(vals), compute_metrics(FactSet(vals)), [], {},
                EvidenceStore(),
            ),
            build_registry(),
        )
        try:
            with patch("app.engine.pipeline.IdentityResolver.resolve", return_value=_resolver()), \
                 patch("app.engine.pipeline.run_rules", return_value=clean):
                pipe.run("600519")
        finally:
            pipe.close()
        rows = db.stage_timings("stage_full")
        self.assertEqual([r["stage"] for r in rows], [s.value for s in STAGE_ORDER])
        for r in rows:
            self.assertGreaterEqual(r["elapsed_ms"], 0)


class CacheStatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pailei-stat-")
        self.root = Path(self.temp.name)
        self.patch = patch.object(settings, "db_path", self.root / "app.db")
        self.patch.start()
        db.init_db()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_bump_stat_accumulates(self):
        db.bump_stat("llm_cache_hits")
        db.bump_stat("llm_cache_hits", 2)
        db.bump_stat("llm_cache_misses")
        s = db.stats()
        self.assertEqual(s["llm_cache_hits"], 3)
        self.assertEqual(s["llm_cache_misses"], 1)

    def test_hit_rate_formula(self):
        db.bump_stat("pdf_cache_hits", 3)
        db.bump_stat("pdf_cache_misses", 1)
        hits = db.stats().get("pdf_cache_hits", 0)
        misses = db.stats().get("pdf_cache_misses", 0)
        self.assertEqual(round(hits / (hits + misses) * 100, 1), 75.0)


class ContentDedupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pailei-blob-")
        self.root = Path(self.temp.name)
        self.patch = patch.object(settings, "files_dir", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _sha(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def test_same_content_single_blob(self):
        content = b"%PDF-same-content-fixture"
        sha = self._sha(content)
        a = self.root / "a.pdf"
        b = self.root / "b.pdf"
        a.write_bytes(content)
        b.write_bytes(content)
        pa = dedup(a, sha)
        pb = dedup(b, sha)
        self.assertEqual(pa, pb)
        self.assertEqual(pa, self.root / "_blob" / f"{sha}.pdf")
        self.assertTrue(pa.exists())
        # 两个路径共享同一 inode（不额外占磁盘）
        self.assertEqual(os.stat(a).st_ino, os.stat(b).st_ino)

    def test_different_content_different_blobs(self):
        a = self.root / "a.pdf"
        b = self.root / "b.pdf"
        a.write_bytes(b"PDF-A")
        b.write_bytes(b"PDF-B")
        self.assertNotEqual(dedup(a, self._sha(b"PDF-A")), dedup(b, self._sha(b"PDF-B")))

    def test_empty_sha_returns_original_path(self):
        p = self.root / "x.pdf"
        p.write_bytes(b"x")
        self.assertEqual(dedup(p, ""), p)


class CleanupPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pailei-clean-")
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(settings, "db_path", self.root / "app.db"),
            patch.object(settings, "reports_dir", self.root / "reports"),
            patch.object(settings, "files_dir", self.root / "files"),
            patch.object(settings, "cache_dir", self.root / "cache"),
        ]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_superseded_reports_identified(self):
        db.save_report("t1", "/tmp/a.html", "/tmp/a.json", {"rule_version": "1.1"})
        db.save_report("t1", "/tmp/a.html", "/tmp/a.json", {"rule_version": "1.1"})  # v2
        db.save_report("t2", "/tmp/b.html", "/tmp/b.json", {"rule_version": "1.1"})  # 唯一版本
        superseded = db.superseded_report_rows()
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["task_id"], "t1")
        self.assertEqual(superseded[0]["version"], 1)

    def test_clear_superseded_reports_keeps_latest(self):
        db.save_report("t1", "/tmp/a.html", "/tmp/a.json", {"rule_version": "1.1"})
        db.save_report("t1", "/tmp/a.html", "/tmp/a.json", {"rule_version": "1.1"})
        removed = db.clear_superseded_reports()
        self.assertEqual(removed, 1)
        # 最新版本保留
        self.assertEqual(db.load_latest_report_paths("t1"), ("/tmp/a.html", "/tmp/a.json"))

    def test_pdf_cache_collected(self):
        cache_dir = self.root / "cache" / "pdf_parse"
        cache_dir.mkdir(parents=True)
        (cache_dir / "one.json").write_text("{}")
        (cache_dir / "two.json").write_text("{}")
        from scripts.cleanup import collect_pdf_cache
        files = collect_pdf_cache()
        self.assertEqual(len(files), 2)

    def test_clear_llm_cache(self):
        db.save_llm_cache("key1", {"data": []})
        self.assertEqual(db.llm_cache_count(), 1)
        self.assertEqual(db.clear_llm_cache(), 1)
        self.assertEqual(db.llm_cache_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
