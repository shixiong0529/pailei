"""验收专用真实扫描；隔离数据库与输出，复用已有 PDF 的只读副本。"""
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import settings
from app.core import db
from app.engine.pipeline import ScanPipeline

OUT = Path(__file__).resolve().parent / "runtime"
OUT.mkdir(exist_ok=True)
settings.db_path = OUT / "live.db"
settings.reports_dir = OUT / "reports"
settings.files_dir = OUT / "files"
for directory in (settings.reports_dir, settings.files_dir):
    directory.mkdir(exist_ok=True)
for p in (ROOT / "data/files").glob("*/*.pdf"):
    dest = settings.files_dir / p.parent.name / p.name
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        shutil.copy2(p, dest)
db.init_db()
rows = []
for query in sys.argv[1:] or ["600519.SH", "00700.HK"]:
    started = time.monotonic()
    scan = ScanPipeline(task_id="fixed11_" + query.replace(".", "_") + "_" + uuid.uuid4().hex[:6])
    print("START", query, flush=True)
    result = scan.run(query)
    row = {"query": query, "task_id": scan.task_id, "status": result.status.value,
           "seconds": round(time.monotonic() - started, 2), "message": result.message,
           "scope": result.payload.get("data_scope"), "summary": result.payload.get("summary"),
           "ai": result.payload.get("ai"), "gaps": result.payload.get("gaps")}
    rows.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    (OUT / "live_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
