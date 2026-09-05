"""只读检查历史产物兼容性，以及仅含可交付源码的独立目录启动。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.report.render import render_inline
from app.core import db
from app.config import settings
rows=[]
for path in sorted((ROOT/'data/reports').glob('*.json')):
    try:
        payload=json.loads(path.read_text());html=render_inline(payload)
        rows.append({'file':path.name,'rendered':bool(html),'error':''})
    except Exception as exc:rows.append({'file':path.name,'rendered':False,'error':str(exc)})
source_files=subprocess.check_output(['git','ls-files','-z','app'],cwd=ROOT).decode().split('\0')
with tempfile.TemporaryDirectory(prefix='pailei-delivery-') as tmp:
    dest=Path(tmp)
    for name in source_files:
        if not name:continue
        target=dest/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
    result=subprocess.run([sys.executable,'-c','from app.main import app; from app.engine.runner import build_registry; print(len(build_registry().rules))'],cwd=dest,text=True,capture_output=True,env={k:v for k,v in os.environ.items() if not k.startswith('LLM_') and k!='DB_PATH'})
    export={'files':len([x for x in source_files if x]),'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
output={'historical_renders':rows,'source_export':export}
(Path(__file__).parent/'delivery_results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
print({'historical_total':len(rows),'historical_rendered':sum(r['rendered'] for r in rows),'source_export':export})
assert result.returncode==0 and all(r['rendered'] for r in rows)
