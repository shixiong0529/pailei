"""FastAPI 应用：页面、API 与后台扫描任务。

任务执行采用进程内线程池（上限可配），任务状态与检查点写入 SQLite，
重启后可从历史任务恢复查看；不引入 Redis/Celery 以避免额外基础设施依赖。
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.core import db
from app.core.http_client import HttpClient
from app.core.models import STAGE_DETAIL, STAGE_ORDER, Stage, TaskStatus
from app.data.eastmoney import EastmoneyClient
from app.data.identity import IdentityResolver
from app.engine.pipeline import ScanPipeline
from app.report.render import render_inline

WEB_DIR = __import__("pathlib").Path(__file__).parent / "web"
TEMPLATE_DIR = WEB_DIR / "templates"

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

MAX_WORKERS = 5
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
_running: dict[str, float] = {}
_lock = threading.Lock()


def render(template_name: str, **context: Any) -> HTMLResponse:
    template = env.get_template(template_name)
    return HTMLResponse(template.render(**context))


# ------------------------------------------------------------------ 页面


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    tasks = db.list_tasks(limit=8)
    return render(
        "home.html",
        app_name=settings.app_name,
        tasks=tasks,
        llm_ready=settings.llm_ready,
        llm_reason="" if settings.llm_ready else "未配置 LLM_API_KEY，AI 解读步骤不参与扫描",
    )


@app.get("/history", response_class=HTMLResponse)
def history():
    return render("history.html", app_name=settings.app_name, tasks=db.list_tasks(limit=100))


@app.get("/admin", response_class=HTMLResponse)
def admin():
    return render(
        "admin.html",
        app_name=settings.app_name,
        sources=db.source_stats(),
        llm=db.llm_stats(),
        tasks=db.task_stats(),
        settings_view={
            "市场": "A 股（沪/深/北） + 港股",
            "财务数据源": "东方财富数据中心",
            "A 股公告源": "巨潮资讯网",
            "港股公告源": "港交所披露易",
            "模型": settings.llm.model if settings.llm_ready else "未配置",
            "公告检索月数": settings.announcement_months,
            "单次原文下载上限": settings.max_pdf_downloads,
            "单文件解析页数上限": settings.max_pdf_pages,
            "任务超时(秒)": settings.scan_timeout_seconds,
            "并行任务上限": MAX_WORKERS,
        },
    )


@app.get("/scan/{task_id}", response_class=HTMLResponse)
def scan_page(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return render(
        "scan.html",
        app_name=settings.app_name,
        task=task,
        stages=[s.value for s in STAGE_ORDER],
        stage_detail={s.value: STAGE_DETAIL[s] for s in STAGE_ORDER},
    )


@app.get("/report/{task_id}", response_class=HTMLResponse)
def report_page(task_id: str):
    payload = db.load_report_payload(task_id)
    if not payload:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return RedirectResponse(url=f"/scan/{task_id}")
    return HTMLResponse(render_inline(payload))


@app.get("/download/{task_id}")
def download_report(task_id: str):
    paths = db.load_latest_report_paths(task_id)
    if not paths:
        raise HTTPException(status_code=404, detail="报告尚未生成")
    html_path = paths[0]
    try:
        content = open(html_path, "rb").read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"报告文件读取失败: {exc}")
    filename = f"report-{task_id}.html"
    return Response(
        content=content,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ API


@app.get("/api/suggest")
def suggest(q: str = Query("", min_length=0)):
    """证券搜索建议。返回候选项，由用户确认证券。"""
    query = (q or "").strip()
    if not query:
        return JSONResponse({"ok": True, "items": []})
    with HttpClient() as client:
        em = EastmoneyClient(client)
        with IdentityResolver(em) as resolver:
            candidates = resolver.search(query, limit=10)
    items = []
    for c in candidates:
        items.append(
            {
                **c.security.to_dict(),
                "score": c.score,
                "match_reason": c.match_reason,
                "value": c.security.secucode,
            }
        )
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/scan")
async def create_scan(request: Request):
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入股票名称或代码")

    # 去重：同一查询在 60 分钟内已完成的任务直接复用
    recent = db.find_recent_task(query, within_minutes=60)
    if recent and not body.get("force"):
        return JSONResponse(
            {
                "ok": True,
                "task_id": recent["task_id"],
                "reused": True,
                "message": "已有 60 分钟内的扫描结果，已直接复用；如需重新扫描请勾选强制刷新",
            }
        )

    task_id = uuid.uuid4().hex[:12]
    db.create_task(task_id, query, params={"force": bool(body.get("force"))})
    with _lock:
        _running[task_id] = time.time()
    executor.submit(_run_task, task_id, query)
    return JSONResponse({"ok": True, "task_id": task_id, "reused": False})


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    facts_count = 0
    docs_count = 0
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n FROM financial_facts WHERE task_id=?", (task_id,)
        ).fetchone()
        facts_count = int(row["n"]) if row else 0
        row = conn.execute(
            "SELECT COUNT(*) n FROM documents WHERE task_id=?", (task_id,)
        ).fetchone()
        docs_count = int(row["n"]) if row else 0
    return JSONResponse(
        {
            "ok": True,
            "task_id": task_id,
            "status": task.get("status"),
            "stage": task.get("stage"),
            "stage_index": task.get("stage_index") or 0,
            "query": task.get("query"),
            "secucode": task.get("secucode"),
            "error": task.get("error"),
            "elapsed_ms": task.get("elapsed_ms"),
            "facts": facts_count,
            "documents": docs_count,
            "has_report": bool(db.load_latest_report_paths(task_id)),
        }
    )


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": settings.app_name,
        "llm_ready": settings.llm_ready,
        "running": len(_running),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ 任务执行


def _run_task(task_id: str, query: str) -> None:
    pipeline = ScanPipeline(task_id=task_id)
    try:
        pipeline.run(query)
    except Exception as exc:  # 兜底，确保任务状态一定被更新
        db.update_task(
            task_id,
            status=TaskStatus.FAILED.value,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    finally:
        with _lock:
            _running.pop(task_id, None)
        pipeline.close()


def create_app() -> FastAPI:
    db.init_db()
    return app
