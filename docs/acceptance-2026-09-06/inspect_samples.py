"""只读检查历史报告、SQLite、线上报告与下载；输出验收证据摘要。"""
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import httpx
from app.report.render import render_inline,risk_signal_score

OUT=Path(__file__).resolve().parent
result={"reports":[],"routes":[],"repository":{}}
conn=sqlite3.connect(f"file:{ROOT/'data/app.db'}?mode=ro",uri=True)
conn.row_factory=sqlite3.Row
result["tasks_by_status"]=[dict(r) for r in conn.execute("select status,count(*) n from scan_tasks group by status")]
for path in sorted((ROOT/"data/reports").glob("*.json")):
    p=json.loads(path.read_text())
    try:
        html=render_inline(p)
        render_error=""
    except Exception as exc:
        html="";render_error=str(exc)
    rlist=[r for d in p["dimensions"] for r in d["results"]]
    evidences=p["evidence"]
    stale=[(r["rule_id"],eid) for r in rlist for eid in r["evidence_ids"] if eid not in evidences]
    dbrow=conn.execute("select payload from reports where task_id=? order by version desc limit 1",(path.stem,)).fetchone()
    result["reports"].append({"id":path.stem,"security":p["security"]["secucode"],
        "name":p["security"]["name"],"generated_at":p["generated_at"],"status":p["scan"]["status"],
        "rule_count":len(rlist),"score":risk_signal_score(p["dimensions"])["score"],
        "risk_count":p["summary"]["risk_count"],"watch_count":p["summary"]["watch_count"],
        "coverage":p["summary"]["coverage"],"unbound_ids":stale,
        "render_error":render_error,"db_json_equal":bool(dbrow and json.loads(dbrow[0])==p),
        "source_files_present":sum(Path(d["local_path"]).exists() for d in p["documents"] if d["local_path"]),
        "scope":p["data_scope"],
        "audit_result":next(r for r in rlist if r["rule_id"]=="OP02"),
        "restricted_cash_result":next(r for r in rlist if r["rule_id"]=="SV07"),
        "truncated_docs":[{"id":d["doc_id"],"title":d["title"],"pages":d["page_count"]} for d in p["documents"] if d["page_count"]>120],
        "rules_without_evidence":[r["rule_id"] for r in rlist if r["status"] in ["发现风险","需要关注"] and not r["evidence_ids"]],
        "ai_failures":p["ai"]["usage"].get("failures",[]),
        "html_has_svg":"<svg" in html,"html_has_timeline":"事件时间线" in html,
        "gaps":p["gaps"],"gaps_displayed":[g for g in p["gaps"] if g in html]})
conn.close()
with httpx.Client(base_url="http://127.0.0.1:8770",timeout=30,trust_env=False) as client:
    for route in ["/","/history","/admin","/api/health","/api/tasks/missing","/report/missing","/download/missing",
                  "/scan/6c688308a030","/report/6c688308a030","/download/6c688308a030"]:
        r=client.get(route)
        result["routes"].append({"path":route,"status":r.status_code,"size":len(r.content)})
    online=client.get("/report/6c688308a030").text
    downloaded=client.get("/download/6c688308a030").text
    strip_time=lambda s:re.sub(r"生成于 [^·<\n]+", "生成于 [TIME] ",s)
    result["report_download"]={"equal":online==downloaded,"equal_without_footer_time":strip_time(online)==strip_time(downloaded),
                               "online_sha256":hashlib.sha256(online.encode()).hexdigest(),
                               "download_sha256":hashlib.sha256(downloaded.encode()).hexdigest()}
result["repository"]["head"]=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
result["repository"]["tracked_data_adapter_files"]=subprocess.check_output(["git","ls-files","app/data"],cwd=ROOT,text=True).splitlines()
result["repository"]["ignore_match"]=subprocess.run(["git","check-ignore","-v","app/data/identity.py"],cwd=ROOT,text=True,capture_output=True).stdout.strip()
with tempfile.TemporaryDirectory(prefix="pailei-clean-checkout-") as target:
    archive=subprocess.check_output(["git","archive","HEAD"],cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(target,filter="data")
    run=subprocess.run([sys.executable,"-c","import app.main"],cwd=target,text=True,capture_output=True)
    result["repository"]["clean_import_returncode"]=run.returncode
    result["repository"]["clean_import_error"]=run.stderr
(OUT/"sample_inspection.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({"reports":len(result["reports"]),"render_success":sum(not r["render_error"] for r in result["reports"]),
                  "db_json_equal":sum(r["db_json_equal"] for r in result["reports"]),"routes":result["routes"],
                  "download":result["report_download"],"repository":result["repository"]},ensure_ascii=False,indent=2))
