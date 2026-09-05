"""SQLite 存储层。

方案 §7 建议使用 PostgreSQL；本地首版改用 SQLite，避免额外基础设施依赖，
schema 保持与关系型模型一致，后续可平滑迁移。表结构覆盖：
公司/证券、扫描任务、财务事实、披露文件、证据、规则结果、报告版本、抓取日志、数据源健康。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_tasks (
    task_id        TEXT PRIMARY KEY,
    query          TEXT NOT NULL,
    secucode       TEXT,
    market         TEXT,
    status         TEXT NOT NULL,
    stage          TEXT,
    stage_index    INTEGER DEFAULT 0,
    progress       TEXT,
    params         TEXT,
    error          TEXT,
    created_at     TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    elapsed_ms     INTEGER,
    rule_version   TEXT,
    data_snapshot  TEXT
);

CREATE TABLE IF NOT EXISTS securities (
    secucode   TEXT PRIMARY KEY,
    task_id    TEXT,
    payload    TEXT
);

CREATE TABLE IF NOT EXISTS financial_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT,
    secucode     TEXT,
    statement    TEXT,
    raw_item     TEXT,
    std_item     TEXT,
    value        REAL,
    unit         TEXT,
    currency     TEXT,
    period_end   TEXT,
    period_type  TEXT,
    fiscal_year  TEXT,
    notice_date  TEXT,
    source_id    TEXT,
    source_url   TEXT,
    extraction   TEXT,
    verified     INTEGER,
    note         TEXT,
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_task ON financial_facts(task_id);
CREATE INDEX IF NOT EXISTS idx_facts_lookup ON financial_facts(task_id, std_item, period_end);

CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT,
    task_id      TEXT,
    secucode     TEXT,
    title        TEXT,
    doc_type     TEXT,
    publish_date TEXT,
    source       TEXT,
    url          TEXT,
    local_path   TEXT,
    sha256       TEXT,
    size_bytes   INTEGER,
    page_count   INTEGER,
    parsed       INTEGER,
    parse_error  TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (task_id, doc_id)
);

CREATE TABLE IF NOT EXISTS evidences (
    evidence_id  TEXT,
    task_id      TEXT,
    doc_id       TEXT,
    title        TEXT,
    quote        TEXT,
    location     TEXT,
    url          TEXT,
    source       TEXT,
    publish_date TEXT,
    fingerprint  TEXT,
    verified     INTEGER,
    verify_note  TEXT,
    PRIMARY KEY (task_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS rule_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT,
    rule_id       TEXT,
    name          TEXT,
    dimension     TEXT,
    status        TEXT,
    severity      TEXT,
    strength      TEXT,
    finding       TEXT,
    why           TEXT,
    metric_snapshot TEXT,
    evidence_ids  TEXT,
    mitigations   TEXT,
    to_verify     TEXT,
    industry_pack TEXT,
    rule_version  TEXT,
    ai_interpreted INTEGER,
    still_effective INTEGER
);

CREATE TABLE IF NOT EXISTS risk_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT,
    event_id     TEXT,
    title        TEXT,
    occurred_date TEXT,
    category     TEXT,
    summary      TEXT,
    source_doc_id TEXT,
    resolved     INTEGER,
    resolution_note TEXT,
    evidence_ids TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT,
    version      INTEGER,
    html_path    TEXT,
    json_path    TEXT,
    created_at   TEXT,
    rule_version TEXT,
    payload      TEXT
);

CREATE TABLE IF NOT EXISTS fetch_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    url         TEXT,
    stage       TEXT,
    ok          INTEGER,
    status_code INTEGER,
    elapsed_ms  INTEGER,
    bytes       INTEGER,
    attempts    INTEGER,
    error       TEXT,
    host        TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_task ON fetch_logs(task_id);

CREATE TABLE IF NOT EXISTS source_health (
    host        TEXT PRIMARY KEY,
    ok_count    INTEGER DEFAULT 0,
    fail_count  INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT,
    step         TEXT,
    model        TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_cny     REAL,
    created_at   TEXT
);
"""

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- 任务


def create_task(task_id: str, query: str, params: dict[str, Any] | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scan_tasks "
            "(task_id, query, status, stage, stage_index, params, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                task_id,
                query,
                "排队",
                "公司识别",
                0,
                json.dumps(params or {}, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    values = [
        json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        for v in fields.values()
    ]
    values.append(task_id)
    with _lock, tx() as conn:
        conn.execute(f"UPDATE scan_tasks SET {keys} WHERE task_id=?", values)


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scan_tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def find_recent_task(query: str, within_minutes: int = 60) -> dict[str, Any] | None:
    """对重复提交去重：同一查询在短时间内且已完成的任务可复用。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM scan_tasks WHERE query=? AND status='完成' "
            "ORDER BY created_at DESC LIMIT 1",
            (query,),
        ).fetchone()
    if not row:
        return None
    task = dict(row)
    created = task.get("created_at") or ""
    try:
        age = (datetime.now() - datetime.fromisoformat(created)).total_seconds() / 60
    except ValueError:
        return None
    return task if age <= within_minutes else None


# ------------------------------------------------------------------- 明细写入


def save_facts(task_id: str, facts: Iterable[Any]) -> int:
    rows = []
    for f in facts:
        rows.append(
            (
                task_id, f.secucode, f.statement.value, f.raw_item, f.std_item, f.value,
                f.unit, f.currency, f.period_end, f.period_type.value, f.fiscal_year,
                f.notice_date, f.source_id, f.source_url, f.extraction,
                1 if f.verified else 0, f.note, f.fetched_at,
            )
        )
    if not rows:
        return 0
    with tx() as conn:
        conn.executemany(
            "INSERT INTO financial_facts (task_id, secucode, statement, raw_item, std_item, value,"
            " unit, currency, period_end, period_type, fiscal_year, notice_date, source_id,"
            " source_url, extraction, verified, note, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def save_documents(task_id: str, docs: Iterable[Any]) -> int:
    rows = [
        (
            task_id, d.doc_id, d.secucode, d.title, d.doc_type, d.publish_date, d.source,
            d.url, d.local_path, d.sha256, d.size_bytes, d.page_count,
            1 if d.parsed else 0, d.parse_error, d.fetched_at,
        )
        for d in docs
    ]
    if not rows:
        return 0
    with tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO documents (task_id, doc_id, secucode, title, doc_type,"
            " publish_date, source, url, local_path, sha256, size_bytes, page_count, parsed,"
            " parse_error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def save_evidences(task_id: str, evidences: Iterable[Any]) -> int:
    rows = [
        (
            task_id, e.evidence_id, e.doc_id, e.title, e.quote, e.location, e.url,
            e.source, e.publish_date, e.fingerprint, 1 if e.verified else 0, e.verify_note,
        )
        for e in evidences
    ]
    if not rows:
        return 0
    with tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO evidences (task_id, evidence_id, doc_id, title, quote,"
            " location, url, source, publish_date, fingerprint, verified, verify_note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def save_rule_results(task_id: str, results: Iterable[Any]) -> int:
    rows = [
        (
            task_id, r.rule_id, r.name, r.dimension.value, r.status.value, r.severity.value,
            r.strength.value, r.finding, r.why, json.dumps(r.metric_snapshot, ensure_ascii=False),
            json.dumps(r.evidence_ids, ensure_ascii=False),
            json.dumps(r.mitigations, ensure_ascii=False),
            json.dumps(r.to_verify, ensure_ascii=False),
            r.industry_pack, r.rule_version, 1 if r.ai_interpreted else 0,
            None if r.still_effective is None else int(r.still_effective),
        )
        for r in results
    ]
    with tx() as conn:
        conn.execute("DELETE FROM rule_results WHERE task_id=?", (task_id,))
        if rows:
            conn.executemany(
                "INSERT INTO rule_results (task_id, rule_id, name, dimension, status, severity,"
                " strength, finding, why, metric_snapshot, evidence_ids, mitigations, to_verify,"
                " industry_pack, rule_version, ai_interpreted, still_effective)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
    return len(rows)


def save_risk_events(task_id: str, events: Iterable[Any]) -> int:
    rows = [
        (
            task_id, e.event_id, e.title, e.occurred_date, e.category, e.summary,
            e.source_doc_id, None if e.resolved is None else int(e.resolved),
            e.resolution_note, json.dumps(e.evidence_ids, ensure_ascii=False),
        )
        for e in events
    ]
    with tx() as conn:
        conn.execute("DELETE FROM risk_events WHERE task_id=?", (task_id,))
        if rows:
            conn.executemany(
                "INSERT INTO risk_events (task_id, event_id, title, occurred_date, category,"
                " summary, source_doc_id, resolved, resolution_note, evidence_ids)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
    return len(rows)


def save_report(task_id: str, html_path: str, json_path: str, payload: dict[str, Any]) -> int:
    with tx() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM reports WHERE task_id=?", (task_id,)
        ).fetchone()
        version = int(row[0]) + 1
        conn.execute(
            "INSERT INTO reports (task_id, version, html_path, json_path, created_at,"
            " rule_version, payload) VALUES (?,?,?,?,?,?,?)",
            (
                task_id, version, html_path, json_path,
                datetime.now().isoformat(timespec="seconds"),
                str(payload.get("rule_version") or "1.0"),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return version


def save_fetch_logs(task_id: str, records: Iterable[Any]) -> None:
    rows = [
        (
            task_id, r.url, r.stage, 1 if r.ok else 0, r.status_code, r.elapsed_ms,
            r.bytes, r.attempts, r.error, r.host,
        )
        for r in records
    ]
    if not rows:
        return
    with tx() as conn:
        conn.executemany(
            "INSERT INTO fetch_logs (task_id, url, stage, ok, status_code, elapsed_ms, bytes,"
            " attempts, error, host) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        for r in records:
            host = r.host or "unknown"
            conn.execute(
                "INSERT INTO source_health (host, ok_count, fail_count, bytes, last_error, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
                " ok_count=ok_count+excluded.ok_count, fail_count=fail_count+excluded.fail_count,"
                " bytes=bytes+excluded.bytes, last_error=excluded.last_error,"
                " updated_at=excluded.updated_at",
                (
                    host, 1 if r.ok else 0, 0 if r.ok else 1, r.bytes,
                    None if r.ok else (r.error or "")[:300],
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )


def save_llm_usage(
    task_id: str, step: str, model: str, tin: int, tout: int, cost: float
) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO llm_usage (task_id, step, model, input_tokens, output_tokens, cost_cny, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (task_id, step, model, tin, tout, cost, datetime.now().isoformat(timespec="seconds")),
        )


# ------------------------------------------------------------------- 明细读取


def load_report_payload(task_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM reports WHERE task_id=? ORDER BY version DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def load_latest_report_paths(task_id: str) -> tuple[str, str] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT html_path, json_path FROM reports WHERE task_id=? ORDER BY version DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return (row["html_path"], row["json_path"]) if row else None


def load_facts(task_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM financial_facts WHERE task_id=? ORDER BY period_end DESC", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def load_documents(task_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE task_id=? ORDER BY publish_date DESC", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def load_evidences(task_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM evidences WHERE task_id=?", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def load_rule_results(task_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rule_results WHERE task_id=?", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def source_stats() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM source_health ORDER BY (ok_count+fail_count) DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def llm_stats() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) tin,"
            " COALESCE(SUM(output_tokens),0) tout, COALESCE(SUM(cost_cny),0) cost"
            " FROM llm_usage"
        ).fetchone()
    return dict(row) if row else {"n": 0, "tin": 0, "tout": 0, "cost": 0.0}


def task_stats() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) n FROM scan_tasks GROUP BY status").fetchall()
        failed = conn.execute(
            "SELECT task_id, query, error, created_at FROM scan_tasks"
            " WHERE status='失败' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows} | {"recent_failures": [dict(r) for r in failed]}
