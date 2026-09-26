"""隔离 Web 验收服务器：合成财务/公告，全流程执行，不访问数据源或模型。"""
from __future__ import annotations
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=ROOT/'docs/project-audit-20260926/runtime/web'
RUNTIME.mkdir(parents=True,exist_ok=True)
os.environ['LLM_API_KEY']=''
os.environ['ENABLE_LLM']='false'
os.environ['ENABLE_NETWORK']='false'
os.environ['DB_PATH']=str(RUNTIME/'app.db')
sys.path.insert(0,str(ROOT))

from app.config import settings
from app.core import db
from app.core.models import Company, Security, Market, Statement, DisclosureDoc
from app.data.identity import IdentityResolver, ResolveResult
from app.data.eastmoney import EastmoneyClient, _a_rows_to_facts, A_BALANCE_MAP, A_INCOME_MAP, A_CASHFLOW_MAP
from app.data.pdftext import ParsedDoc
from app.engine.pipeline import ScanPipeline
from app.engine.rules.base import EvidenceStore

settings.files_dir=RUNTIME/'files'
settings.cache_dir=RUNTIME/'cache'
settings.reports_dir=RUNTIME/'reports'
db.init_db()
RAW=RUNTIME/'raw';RAW.mkdir(exist_ok=True)
rows=[]
for year in (2023,2024,2025):
    rows.append({'SECUCODE':'600519.SH','REPORT_DATE':f'{year}-12-31',
        'NOTICE_DATE':f'{year+1}-03-31','TOTAL_ASSETS':10000000,'TOTAL_LIABILITIES':9000000,
        'TOTAL_EQUITY':1000000,'MONETARYFUNDS':100000,'SHORT_LOAN':2000000,'LONG_LOAN':1000000,
        'TOTAL_CURRENT_ASSETS':2000000,'TOTAL_CURRENT_LIAB':5000000,'ACCOUNTS_RECE':2000000,
        'INVENTORY':1000000,'TOTAL_OPERATE_INCOME':5000000,'PARENT_NETPROFIT':500000,
        'DEDUCT_PARENT_NETPROFIT':100000,'NETCASH_OPERATE':-100000,'CONSTRUCT_LONG_ASSET':200000})
(RAW/'financial_rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
disclosure='本公司未能按期偿还借款本金人民币200万元。\n本公司内部控制存在重大缺陷。'
(RAW/'disclosure.txt').write_text(disclosure)
(RUNTIME/'source_manifest.json').write_text(json.dumps({'source':'synthetic offline fixture; not an issuer disclosure',
    'generated_at':datetime.now().isoformat(),'periods':[r['REPORT_DATE'] for r in rows],
    'script':'workpapers/project_audit_20260926/web_fixture.py','raw':['raw/financial_rows.json','raw/disclosure.txt'],
    'mapping':'A_BALANCE_MAP/A_INCOME_MAP/A_CASHFLOW_MAP','network':False,'model':False},ensure_ascii=False,indent=2))

def resolve(self,query):
    security=Security('600519',Market.A,'合成排雷样本','600519.SH',org_name='合成排雷样本（非真实公司结论）',industry='测试制造业')
    return ResolveResult(True,selected=security,company=Company('fixture','合成排雷样本',[security]))

def statements(self,secucode,years=None):
    result=[]
    for row in rows:
        item=dict(row,_raw_ref='raw/financial_rows.json')
        for statement,mapping in [(Statement.BALANCE,A_BALANCE_MAP),(Statement.INCOME,A_INCOME_MAP),(Statement.CASHFLOW,A_CASHFLOW_MAP)]:
            result.extend(_a_rows_to_facts(item,statement,mapping,'synthetic fixture'))
    return result

def collect(self,security,start,end):
    document=DisclosureDoc('fixture:disclosure',security.secucode,'合成测试公告：债务逾期与内控缺陷',
        '2026-06-30','fixture','https://example.org/synthetic-disclosure',doc_type='其他公告',
        local_path=str(RAW/'disclosure.txt'))
    return [document],{'source':'fixture','total':1,'fetched':1,'range':f'{start} ~ {end}'}

def build_evidence(self,security,docs):
    docs[0].parsed=True;docs[0].page_count=1
    return EvidenceStore(),{docs[0].doc_id:ParsedDoc(docs[0].doc_id,1,[(1,disclosure)],False)}

IdentityResolver.resolve=resolve
EastmoneyClient.a_statements=statements
ScanPipeline._collect_documents=collect
ScanPipeline._build_evidence=build_evidence

if __name__=='__main__':
    import uvicorn
    from app.main import app
    uvicorn.run(app,host='127.0.0.1',port=18770,log_level='warning')
