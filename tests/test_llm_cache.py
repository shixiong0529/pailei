"""V1.2 阶段 1 测试：模型结果缓存与可复现性。

全离线、确定性。覆盖：
- 缓存命中一致性、命中不重复调用、命中不计成本/次数；
- 提示词/模型/温度等参数变化失效；
- 截断、HTTP 错误、无法解析、结构非法不缓存；
- 同键并发去重；
- 提示词版本变化失效。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings, LLMConfig  # noqa: E402
from app.core import db  # noqa: E402
from app.llm.adapter import LLMAdapter, LLMResult  # noqa: E402
from app.llm import cache  # noqa: E402


def _body(data, finish_reason="stop", status=200):
    return {
        "status": status,
        "data": data,
        "finish_reason": finish_reason,
    }


class _FakeResp:
    def __init__(self, status_code, content, finish_reason):
        self.status_code = status_code
        self._content = content
        self._finish = finish_reason

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}, "finish_reason": self._finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def make_http(counter, responder):
    class _FakeHttpClient:
        def __init__(self):
            self.deadline = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *args, **kwargs):
            counter[0] += 1
            return responder(*args, **kwargs)

    return _FakeHttpClient


class LlmCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pailei-cache-")
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(settings, "db_path", self.root / "app.db"),
            patch.object(settings, "reports_dir", self.root / "reports"),
            patch.object(settings, "files_dir", self.root / "files"),
        ]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _llm(self, **cfg) -> LLMAdapter:
        defaults = dict(api_key="fixture", budget_cny=100.0)
        defaults.update(cfg)
        return LLMAdapter(LLMConfig(**defaults))

    def _ok_responder(self, data, finish_reason="stop", status=200):
        content = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
        return lambda *a, **k: _FakeResp(status, content, finish_reason)

    def test_cache_hit_returns_identical_data_no_extra_call(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            r1 = llm.chat_json("system", "user", step="interpret")
            r2 = llm.chat_json("system", "user", step="interpret")
        self.assertEqual(counter[0], 1)
        self.assertTrue(r1.ok)
        self.assertFalse(r1.cached)
        self.assertTrue(r2.ok)
        self.assertTrue(r2.cached)
        self.assertEqual(r1.data, r2.data)

    def test_cache_invalidated_by_prompt_change(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            llm.chat_json("system", "user-v1", step="interpret")
            llm.chat_json("system", "user-v2", step="interpret")
        self.assertEqual(counter[0], 2)

    def test_cache_invalidated_by_temperature(self):
        counter = [0]
        llm = self._llm(temperature=0.1)
        llm2 = self._llm(temperature=0.9)
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            llm.chat_json("s", "u", step="interpret")
            llm2.chat_json("s", "u", step="interpret")
        self.assertEqual(counter[0], 2)

    def test_cache_invalidated_by_model_change(self):
        counter = [0]
        llm = self._llm(model="model-a")
        llm2 = self._llm(model="model-b")
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            llm.chat_json("s", "u", step="interpret")
            llm2.chat_json("s", "u", step="interpret")
        self.assertEqual(counter[0], 2)

    def test_truncated_response_not_cached(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder('[{"rule_id":', finish_reason="length"))):
            r1 = llm.chat_json("s", "u", step="interpret")
            r2 = llm.chat_json("s", "u", step="interpret")
        self.assertFalse(r1.ok)
        self.assertEqual(counter[0], 2, "截断响应不得缓存复用")

    def test_http_error_not_cached(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([], status=500))):
            llm.chat_json("s", "u", step="interpret")
            llm.chat_json("s", "u", step="interpret")
        self.assertEqual(counter[0], 2)

    def test_unparseable_not_cached(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder("not json at all"))):
            r1 = llm.chat_json("s", "u", step="interpret")
            r2 = llm.chat_json("s", "u", step="interpret")
        self.assertFalse(r1.ok)
        self.assertEqual(counter[0], 2)

    def test_non_list_structure_not_cached(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder({"not": "a list"}))):
            r1 = llm.chat_json("s", "u", step="interpret")
            r2 = llm.chat_json("s", "u", step="interpret")
        self.assertTrue(r1.ok)
        self.assertFalse(r1.cached)
        self.assertEqual(counter[0], 2, "结构非法（非数组）不得缓存复用")

    def test_concurrent_same_key_single_call(self):
        counter = [0]
        gate = threading.Barrier(2)

        def responder(*a, **k):
            time.sleep(0.02)
            return _FakeResp(200, json.dumps([{"rule_id": "one"}]), "stop")

        llm = self._llm()
        results = []

        def run():
            with patch("app.llm.adapter.HttpClient", make_http(counter, responder)):
                results.append(llm.chat_json("same-system", "same-user", step="interpret"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(counter[0], 1, "同键并发不得重复调用模型")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(results[0].data, results[1].data)

    def test_cache_hit_does_not_count_call_or_cost(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            llm.chat_json("s", "u", step="interpret")
            llm.chat_json("s", "u", step="interpret")
        summary = llm.usage_summary()
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["cache_hits"], 1)
        self.assertAlmostEqual(summary["spent_cny"], llm.spent_cny, places=6)

    def test_usage_summary_reports_cache_hits(self):
        counter = [0]
        llm = self._llm()
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            llm.chat_json("s", "u", step="interpret")
        self.assertIn("cache_hits", llm.usage_summary())

    def test_prompt_version_bump_invalidates(self):
        counter = [0]
        llm = self._llm()
        from app.llm import adapter
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            with patch.object(adapter, "PROMPT_VERSION", "1"):
                llm.chat_json("s", "u", step="interpret")
            with patch.object(adapter, "PROMPT_VERSION", "2"):
                llm.chat_json("s", "u", step="interpret")
        self.assertEqual(counter[0], 2)

    def test_cache_key_no_api_key_leak(self):
        cfg = LLMConfig(api_key="SECRET-KEY", base_url="https://api.example.com/v1", model="m")
        key = cache.compute_cache_key(cfg, "sys", "usr", step="interpret")
        self.assertNotIn("SECRET", key)
        stored = db.get_llm_cache("nonexistent")
        self.assertIsNone(stored)

    def test_cache_value_contains_no_secret(self):
        llm = self._llm(api_key="SUPER-SECRET")
        counter = [0]
        with patch("app.llm.adapter.HttpClient", make_http(counter, self._ok_responder([{"rule_id": "one"}]))):
            llm.chat_json("s", "u", step="interpret")
        with db.connect() as conn:
            rows = conn.execute("SELECT payload FROM llm_cache").fetchall()
        for row in rows:
            self.assertNotIn("SUPER-SECRET", row["payload"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
