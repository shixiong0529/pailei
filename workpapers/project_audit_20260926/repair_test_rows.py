"""隔离故障恢复：只备份/移除本次测试生成且没有 scan_tasks 父行的孤立记录。"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.config import settings
from app.data.provenance import _write_once

TASK_ID = '2eac56a1e56c'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    raw_dir=ROOT/'data/raw/project_audit_20260926'
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM scan_tasks WHERE task_id=?',(TASK_ID,)).fetchone():
            raise RuntimeError('Refusing: task has a parent row, not the audited orphan')
        tables=[]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            name=row[0]
            if not name.replace('_','').isalnum():continue
            if 'task_id' in {r[1] for r in conn.execute(f'PRAGMA table_info({name})')}:
                tables.append(name)
        snapshot={table:[dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE task_id=?',(TASK_ID,))] for table in tables}
        counts={k:len(v) for k,v in snapshot.items()}
        if not args.execute:
            print(json.dumps({'preview':counts},ensure_ascii=False));conn.rollback();return
        body=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,indent=2).encode()
        backup=raw_dir/'orphan_task_rows_v1.json'
        _write_once(backup,body)
        manifest={'source':str(settings.db_path),'task_id':TASK_ID,'extracted_at':datetime.now().isoformat(),
                  'filter':'task_id = 2eac56a1e56c; parent scan_tasks row absent',
                  'raw_backup':str(backup.relative_to(ROOT)),'sha256':hashlib.sha256(body).hexdigest(),
                  'counts':counts,'restore':'Insert the backed up rows with their original columns and IDs in a transaction; no user tasks were included.'}
        _write_once(ROOT/'data/source_manifest/project_audit_test_repair_v1.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
        for table in tables:conn.execute(f'DELETE FROM {table} WHERE task_id=?',(TASK_ID,))
        remaining={table:conn.execute(f'SELECT COUNT(*) FROM {table} WHERE task_id=?',(TASK_ID,)).fetchone()[0] for table in tables}
        if any(remaining.values()):raise RuntimeError('orphan reconciliation failed')
        conn.commit()
        result={'task_id':TASK_ID,'backup':manifest['raw_backup'],'removed':counts,'remaining':remaining,
                'at':datetime.now().isoformat(),'raw_snapshots':'retained; not deleted'}
        _write_once(ROOT/'workpapers/project_audit_20260926/test_isolation_repair.json',json.dumps(result,ensure_ascii=False,indent=2).encode())
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
