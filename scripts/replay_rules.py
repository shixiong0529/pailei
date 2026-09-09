"""Offline rule replay on stored facts and PDF text; no network or model calls.

Usage: python scripts/replay_rules.py TASK_ID [TASK_ID ...]
Outputs are validation artifacts, not fresh scans. Original reports/database remain unchanged.
"""
import sys, json, dataclasses, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings
from app.core import db
from app.core.models import FinancialFact, DisclosureDoc, Security, Market, PeriodType, Statement, RuleStatus
from app.data.pdftext import ParsedDoc
from app.engine.normalize import FactSet
from app.engine.metrics import compute_metrics
from app.engine.rules.base import RuleContext, EvidenceStore
from app.engine.runner import build_registry, run_rules
from app.report.render import render_inline, risk_signal_score
root = ROOT
outputs = root/'docs/v13-upgrade/runtime'
outputs.mkdir(parents=True, exist_ok=True)
if not sys.argv[1:]:
    raise SystemExit('Usage: python scripts/replay_rules.py TASK_ID [TASK_ID ...]')
results = []
for tid in sys.argv[1:]:
    if not tid.isalnum():
        raise SystemExit('Task ID must be alphanumeric')
    old = json.loads((root/f'data/reports/{tid}.json').read_text())
    def allowed(cls, row):
        names = {f.name for f in dataclasses.fields(cls)}
        return {k:v for k,v in row.items() if k in names and v is not None}
    facts = []
    for row in db.load_facts(tid):
        f = allowed(FinancialFact, row); f['value'] = row.get('value'); f['statement'] = Statement(f['statement']); f['period_type'] = PeriodType(f['period_type'])
        facts.append(FinancialFact(**f))
    docs = [DisclosureDoc(**allowed(DisclosureDoc, d)) for d in db.load_documents(tid)]
    parsed = {}
    for d in docs:
        pages = {}; n = 0
        for f in sorted((root/'data/cache/pdf_parse').glob(f'{d.sha256}.1.*.json')) if d.sha256 else []:
            cache = json.loads(f.read_text())
            if cache.get('error'): continue
            pages.update({int(i):text for i,text in cache['pages']})
            n = max(n, cache.get('page_count', 0))
        if pages:
            parsed[d.doc_id] = ParsedDoc(d.doc_id, n, sorted(pages.items()), len(pages)<n)
    secdata = allowed(Security, old['security']); secdata['market'] = Market(secdata['market'])
    sec = Security(**secdata); fs = FactSet(facts)
    ctx = RuleContext(sec, sec.market, fs, compute_metrics(fs, market=sec.market), docs, parsed, EvidenceStore(), industry_pack=old['industry_pack'])
    start = time.perf_counter(); out = run_rules(ctx, build_registry()); elapsed = time.perf_counter()-start
    dims = [{'dimension': dim, 'results':[o.to_result().to_dict() for o in out.outcomes if o.rule.dimension.value==dim]} for dim in dict.fromkeys(o.rule.dimension.value for o in out.outcomes)]
    new = dict(old);new.update(report_version='1.3', rule_version='1.3', scoring_version='2', dimensions=dims, task_id='replay-'+tid,
                             rule_workpapers={o.rule.rule_id:o.workpaper for o in out.outcomes}, evidence={eid:e.to_dict() for eid,e in ctx.evidence.items.items()})
    new['summary'] = dict(old['summary'], coverage=out.coverage.to_dict(), risk_count=len(out.risks()), watch_count=len(out.watches()), insufficient_count=len(out.insufficient()))
    new['notes'] = ['验证用历史资料离线重放：未重新获取公告，不是截至今日的投资判断；使用旧版缓存的已解析页段；历史 API 原始响应缺失时不能补造。']
    new['data_scope'] = dict(new['data_scope'], evidence_count=len(ctx.evidence.items), evidence_verified=sum(e.verified for e in ctx.evidence.items.values()))
    new['timeline']=[];new['lifecycles']=[];new['pending_clues']=[]
    from app.engine.pipeline import ScanPipeline
    new['missing_data']=ScanPipeline._collect_missing(out);new['unsupported_data']=ScanPipeline._collect_unsupported(out)
    new['capability_summary']=ScanPipeline._capability_summary(out)
    new['summary']['top_findings']=[]
    new['summary']['highest_severity']='未定'
    new['summary']['conclusion']='验证用历史资料离线重放，不是新扫描。'
    new['scan'] = dict(new['scan'], elapsed_seconds=round(elapsed,3))
    new['ai'] = dict(old.get('ai', {}));new['ai']['notes']=['本次重放未调用模型']
    new['ai']['verification']=[]
    new['ai']['usage']={'available':False,'reason':'历史资料离线重放，未调用模型','model':'','calls':0,'spent_cny':0,'failures':[]}
    (outputs/f'{tid}.json').write_text(json.dumps(new,ensure_ascii=False,indent=2))
    (outputs/f'{tid}.html').write_text(render_inline(new))
    summary={'task_id':tid,'name':sec.name,'facts':len(facts),'parsed_docs':len(parsed),'rules':len(out.outcomes),
             'rule_seconds':round(elapsed,3),'coverage':out.coverage.to_dict(),'score':risk_signal_score(dims,deduplicate=True)['score'],
             'new_findings':[{ 'id':o.rule.rule_id,'status':o.status.value,'finding':o.finding} for o in out.outcomes if o.rule.rule_id in ['SV09','GV07','GV08','FQ13','FQ14','OP06','OP07','SV10']]}
    results.append(summary)
(outputs/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
for r in results: print(json.dumps({k:v for k,v in r.items() if k not in ('coverage','new_findings')},ensure_ascii=False))
