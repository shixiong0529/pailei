"""界面验收服务：真实页面/API；测试搜索响应与后台任务替身，无外部网络/模型调用。"""
import shutil
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.config import settings
OUT=Path(__file__).resolve().parent/"runtime"
settings.db_path=OUT/"browser.db"
settings.enable_llm=False
if not settings.db_path.exists():
    shutil.copy2(OUT/"live.db",settings.db_path)
from app import main
from app.core import db
from app.core.models import Security,Market,now_iso
from app.data.identity import Candidate

def suggestions(self,query,limit=12):
    if query=="AUDIT_XSS":
        s=Security("600519",Market.A,'<img src=x onerror="document.body.dataset.auditXss=\'executed\'">',"600519.SH")
    elif "00700" in query or "腾讯" in query:
        s=Security("00700",Market.HK,"腾讯控股","00700.HK")
    else:
        s=Security("600519",Market.A,"贵州茅台","600519.SH")
    return [Candidate(s,100,"代码精确匹配")]

def record_submission(task_id,query):
    db.update_task(task_id,status="失败",error="验收替身：已记录提交参数，未发起扫描",finished_at=now_iso())
    with main._lock:main._running.pop(task_id,None)

main.IdentityResolver.search=suggestions
main._run_task=record_submission
if __name__=="__main__":
    import uvicorn
    uvicorn.run(main.app,host="127.0.0.1",port=8771,log_level="warning")
