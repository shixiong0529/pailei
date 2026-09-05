"""验收补充测试：断言期望的正确行为。当前失败即诊断证据，不修改业务代码。

全离线，SQLite/文件写入均在 TemporaryDirectory，外部 HTTP 均由 MockTransport 替代。
"""
import asyncio
import copy
import importlib.util
import json
import math
import socket
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from jinja2 import StrictUndefined
from fastapi.testclient import TestClient
from app.config import settings, LLMConfig
from app.core import db
from app.core.http_client import HttpClient, FetchError, FetchRecord, assert_safe_url
from app.core.models import *
from app.data.cninfo import _classify
from app.data.hkexnews import _classify_hk
from app.data.eastmoney import _limit_years, _a_rows_to_facts, A_BALANCE_MAP, EastmoneyClient
from app.data.identity import IdentityResolver
from app.data.pdftext import ParsedDoc, build_evidence, verify_evidence, scan_audit_opinions
from app.engine.normalize import FactSet
from app.engine.metrics import compute_metrics
from app.engine.rules.base import RuleContext, EvidenceStore
from app.engine.runner import build_registry, run_rules, attach_evidence, ai_verify
from app.engine.pipeline import ScanPipeline
from app.llm.adapter import LLMAdapter, LLMResult
from app.report.render import _env, render_inline, risk_signal_score

spec = importlib.util.spec_from_file_location("original_tests", ROOT / "tests/run_tests.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
fact = baseline.fact


def context(values=None, docs=None, parsed=None, pack="general"):
    fs = FactSet(values or [])
    return RuleContext(Security("000001", Market.A, "测试公司", "000001.SZ"),
                       Market.A, fs, compute_metrics(fs), docs or [], parsed or {},
                       EvidenceStore(), industry_pack=pack)


def rule(rid):
    return next(r for r in build_registry().rules if r.rule_id == rid)


def doc(title="年度报告", kind="年报", did="doc1"):
    return DisclosureDoc(did, "000001.SZ", title, "2026-06-30", "fixture",
                         "https://example.org/report.pdf", doc_type=kind)


class AuditChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pailei-audit-")
        self.tmp_path = Path(self.tmp.name)
        self.patches = [patch.object(settings, "db_path", self.tmp_path / "test.db"),
                        patch.object(settings, "reports_dir", self.tmp_path / "reports"),
                        patch.object(settings, "files_dir", self.tmp_path / "files")]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_01_fully_restricted_cash_is_zero(self):
        ctx = context([fact(k, "2026-06-30", v) for k,v in
                       {"cash":100., "restricted_cash":100., "short_term_borrowings":50.}.items()])
        self.assertEqual(ctx.metrics.get("cash_to_short_debt"), 0.,
                         "全部现金受限时应为0，当前回退为账面现金，得到2倍")

    def test_02_missing_restricted_cash_not_normal(self):
        ctx = context([fact("cash", "2026-06-30", 100.)])
        o = rule("SV07").evaluate(ctx)
        self.assertEqual(o.status, RuleStatus.INSUFFICIENT, o.finding + ";" + o.why)

    def test_03_negative_profit_positive_cash_not_inverted(self):
        ctx = context([fact("net_profit", "2026-06-30", -100.),fact("ocf", "2026-06-30", 50.)])
        o = rule("FQ01").evaluate(ctx)
        self.assertNotIn("净利润为正而经营现金流为负", o.why)

    def test_04_both_negative_not_healthy_profit_cover(self):
        ctx = context([fact("net_profit", "2026-06-30", -100.),fact("ocf", "2026-06-30", -200.)])
        self.assertNotEqual(rule("FQ01").evaluate(ctx).status, RuleStatus.NORMAL)

    def test_05_small_receivables_materiality(self):
        vals = [fact(k, "2026-06-30",v,PeriodType.INTERIM) for k,v in
                {"accounts_receivable":1.,"total_assets":1000.,"revenue":100.}.items()]
        vals += [fact("accounts_receivable","2025-06-30",.1,PeriodType.INTERIM),
                 fact("revenue","2025-06-30",100.,PeriodType.INTERIM)]
        ctx=context(vals)
        self.assertEqual(ctx.metrics.get("ar_to_assets"),.001)
        self.assertEqual(rule("FQ03").evaluate(ctx).status,RuleStatus.NORMAL)

    def test_06_audit_positive_signal_reaches_rule(self):
        d=doc(kind="审计")
        p=ParsedDoc(d.doc_id,1,[(1,"由于上述事项的重要性，我们无法表示意见。")],False)
        self.assertTrue(scan_audit_opinions(p))
        o=rule("OP02").evaluate(context(docs=[d],parsed={d.doc_id:p}))
        self.assertEqual(o.status,RuleStatus.RISK,o.finding)

    def test_07_traditional_audit_signal(self):
        p=ParsedDoc("hk",1,[(1,"由於上述事項的重要性，我們無法表示意見。")],False)
        self.assertTrue(scan_audit_opinions(p),"港股繁体无法表示意见未识别")

    def test_08_empty_pdf_not_normal_audit(self):
        d=doc(kind="审计")
        p=ParsedDoc(d.doc_id,1,[(1,"")],False)
        self.assertEqual(rule("OP02").evaluate(context(docs=[d],parsed={d.doc_id:p})).status,
                         RuleStatus.INSUFFICIENT)

    def test_09_prefix_only_quote_must_fail(self):
        prefix="这是一段真实的财务报告前言文字" * 4
        p=ParsedDoc("d",1,[(1,prefix+"实际没有违约。")],False)
        ev=Evidence("d:p1","d","报告",prefix+"公司已确认债务违约十亿元。",location="第 1 页",fingerprint="tampered")
        verify_evidence(ev,p)
        self.assertFalse(ev.verified,"前40字匹配即通过，伪造后半段与错误指纹均被接受")

    def test_10_same_page_topic_evidence_not_overwritten(self):
        d=doc()
        p=ParsedDoc(d.doc_id,1,[(1,"应收账款"+"甲"*1100+"存货"+"乙"*1100)],False)
        st=EvidenceStore()
        a=build_evidence(d,p,["应收账款"]); b=build_evidence(d,p,["存货"])
        aid=st.add(a,topics=["应收"]); st.add(b,topics=["存货"])
        self.assertIn("应收账款",st.get(aid).quote)

    def test_11_precise_evidence_reverify_by_doc_id(self):
        p=ParsedDoc("doc1",1,[(1,"真实审计意见")],False)
        e=Evidence("doc1:loc123","doc1","审计报告","真实审计意见",location="第 1 页")
        st=EvidenceStore();st.add(e)
        pipe=ScanPipeline("test")
        try: self.assertEqual(pipe._reverify_evidence(st,{"doc1":p}),1)
        finally: pipe.close()

    def test_12_unrelated_quote_not_confirmed_risk(self):
        ctx=context([fact("net_profit","2026-06-30",-100.)])
        ctx.evidence.add(Evidence("old:p1","old","2020年度报告","公司基本情况及通讯地址",verified=True),doc_type="年报")
        o=rule("FQ08").evaluate(ctx); attach_evidence(o,ctx)
        self.assertNotEqual(o.strength,EvidenceStrength.CONFIRMED)

    def test_13_cninfo_classification_preserves_risk(self):
        for title,expected in [("关于2025年年度报告的更正公告","财务更正"),
                               ("关于2025年年度报告的问询函","监管问询"),
                               ("关于聘任会计师事务所的公告","审计机构"),
                               ("2026年半年度业绩预告","业绩预告")]:
            with self.subTest(title=title): self.assertEqual(_classify(title),expected)

    def test_14_hk_warning_not_swallowed_by_results(self):
        self.assertEqual(_classify_hk("盈利警告 - 預期年度業績錄得虧損",""),"盈利警告")

    def test_15_resolved_freeze_not_current_high_risk(self):
        d=doc("关于公司全部资产解除冻结的公告",_classify("关于公司全部资产解除冻结的公告"))
        o=rule("RG05").evaluate(context(docs=[d]))
        self.assertNotEqual(o.status,RuleStatus.RISK,o.finding)

    def test_16_missing_previous_year_not_yoy(self):
        fs=FactSet([fact("revenue","2026-06-30",200.,PeriodType.INTERIM),
                    fact("revenue","2024-06-30",100.,PeriodType.INTERIM)])
        self.assertIsNone(compute_metrics(fs).get("revenue_yoy"))

    def test_17_currency_mismatch_not_compared(self):
        fs=FactSet([fact("revenue","2026-06-30",200.,PeriodType.INTERIM,currency="HKD"),
                    fact("revenue","2025-06-30",100.,PeriodType.INTERIM,currency="CNY")])
        self.assertIsNone(compute_metrics(fs).get("revenue_yoy"))

    def test_18_five_complete_years_plus_latest(self):
        fs=[fact("revenue",f"{y}-12-31",100.) for y in range(2021,2026)]
        fs.append(fact("revenue","2026-06-30",100.,PeriodType.INTERIM))
        self.assertEqual(len(_limit_years(fs,5)),6)

    def test_19_fact_identity_retains_scope(self):
        a=fact("revenue","2026-06-30",100.,consolidated=True)
        b=fact("revenue","2026-06-30",20.,consolidated=False)
        fs=FactSet([a,b])
        self.assertEqual(fs.value("revenue","2026-06-30"),100.,"母公司覆盖合并数据")

    def test_20_hk_percentage_normalized_to_ratio(self):
        em=EastmoneyClient(Mock())
        # 当日真实接口 source_fields.json：ROE_AVG=9.966353785254，单位为百分数。
        with patch.object(em,"query_all",return_value=[{"REPORT_DATE":"2026-06-30","DATE_TYPE_CODE":"002","ROE_AVG":9.966353785254}]):
            fs=FactSet(em._hk_indicators("00700.HK","HKD"))
        self.assertAlmostEqual(compute_metrics(fs).get("roe_avg"),.09966353785254)

    def test_21_missing_goodwill_denominator_not_normal(self):
        o=rule("OP04").evaluate(context([fact("goodwill","2026-06-30",100.)]))
        self.assertEqual(o.status,RuleStatus.INSUFFICIENT,o.finding)

    def test_22_missing_advance_not_treated_as_zero(self):
        vals=[fact("total_assets","2026-06-30",100.),fact("total_liabilities","2026-06-30",80.)]
        self.assertEqual(rule("RE01").evaluate(context(vals,pack="realestate")).status,RuleStatus.INSUFFICIENT)

    def test_23_ssrf_redirect_revalidated(self):
        visited=[]
        def handle(req):
            visited.append(str(req.url))
            if req.url.host=="public.test": return httpx.Response(302,headers={"location":"http://127.0.0.1/private"})
            return httpx.Response(200,text="private fixture")
        c=HttpClient(); c._client.close()
        c._client=httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=True)
        try:
            with patch("app.core.http_client.socket.getaddrinfo",return_value=[(socket.AF_INET,1,6,"",("93.184.216.34",0))]):
                c.request("GET","http://public.test/start",retries=1)
            self.assertFalse(any("127.0.0.1" in u for u in visited),str(visited))
        finally:c.close()

    def test_24_ipv6_mapped_loopback_rejected(self):
        with self.assertRaises(FetchError): assert_safe_url("http://[::ffff:127.0.0.1]/")

    def test_25_template_really_strict(self):
        self.assertIs(_env().undefined,StrictUndefined)

    def test_26_report_displays_actual_gap(self):
        p=baseline.TestReportRendering._payload();p["gaps"]=["AUDIT_GAP_最新年报下载失败"]
        self.assertIn("AUDIT_GAP_最新年报下载失败",render_inline(p))

    def test_27_zero_coverage_not_reassuring(self):
        p=baseline.TestReportRendering._payload()
        p["summary"].update(risk_count=0,watch_count=0,insufficient_count=36)
        p["summary"]["coverage"].update(evaluated=0,applicable=36,insufficient=36)
        html=render_inline(p)
        self.assertNotIn("风险信号评分 · 未发现明显风险信号",html)

    def test_28_llm_enabled_switch(self):
        with patch.object(settings,"enable_llm",True):
            self.assertFalse(LLMAdapter(LLMConfig(enabled=False,api_key="fixture")).available)

    def test_29_llm_budget_partial_failure_visible(self):
        llm=LLMAdapter(LLMConfig(api_key="fixture",budget_cny=.35))
        def chat(*a,**kw):
            llm.spent_cny+=.1
            return LLMResult(True,data=[{"rule_id":"one"}],cost_cny=.1)
        with patch.object(llm,"chat_json",side_effect=chat):
            r=llm._run_batched([[1],[2]],lambda b:("s","u"),"test")
        self.assertTrue(r.ok)
        self.assertTrue(llm.failures or r.error or r.skipped_reason,"预算导致第2批取消但无任何失败说明")

    def test_30_network_disable_is_honored(self):
        c=HttpClient();c._client.close()
        visited=[]
        c._client=httpx.Client(transport=httpx.MockTransport(lambda req:(visited.append(str(req.url)) or httpx.Response(200,json={}))))
        try:
            with patch.object(settings,"enable_network",False),patch("app.core.http_client.assert_safe_url",return_value="https://example.org/"):
                try:c.get_json("https://example.org/")
                except FetchError:pass
            self.assertEqual(visited,[])
        finally:c.close()

    def test_31_llm_event_doc_id_must_exist(self):
        d=doc();p=ParsedDoc("doc1",1,[(1,"正常报告")],False)
        pipe=ScanPipeline("event_test")
        fake=SimpleNamespace(available=True,extract_events=lambda x:LLMResult(True,data=[{"doc_id":"nonexistent","title":"虚构事件"}]))
        pipe.llm=fake
        try:self.assertEqual(pipe._extract_events([d],{"doc1":p}),[])
        finally:pipe.close()

    def test_32_llm_parse_failure_nonfatal(self):
        pipe=ScanPipeline("event_test")
        pipe.llm=SimpleNamespace(available=True,extract_events=lambda x:LLMResult(True,data=[]))
        try:
            ev=pipe._extract_events([doc()],{"doc1":ParsedDoc("doc1",0,[],False,error="bad pdf")})
            self.assertEqual(ev,[])
        finally:pipe.close()

    def test_33_ai_downgrade_recomputes_coverage(self):
        ctx=context([fact("net_profit","2026-06-30",-100.)])
        ctx.evidence.add(Evidence("d:p1","d","年报","缺乏对应数字",verified=True),doc_type="年报")
        out=run_rules(ctx,build_registry())
        before=out.coverage.insufficient
        fake=SimpleNamespace(available=True,config=SimpleNamespace(verify_enabled=True),
                             verify=lambda x:LLMResult(True,data=[{"rule_id":"FQ08","suggested_status":"降级为数据不足"}]))
        ai_verify(out,ctx,fake)
        self.assertEqual(out.coverage.insufficient,before+1)

    def test_34_financial_fact_metadata_survives_db(self):
        db.create_task("t","test")
        f=fact("revenue","2026-06-30",100.,PeriodType.INTERIM,period_start="2026-01-01",audited=False,consolidated=True)
        db.save_facts("t",[f]);row=db.load_facts("t")[0]
        self.assertTrue({"period_start","audited","consolidated"} <= set(row))

    def test_35_log_counts_not_duplicated(self):
        r=FetchRecord("https://example.org","test",True,bytes=1,host="example.org")
        logs=[r];db.save_fetch_logs("t",logs);db.save_fetch_logs("t",logs)
        self.assertEqual(db.source_stats()[0]["ok_count"],1)

    def test_36_industry_pack_saved_correctly(self):
        ctx=context(pack="bank")
        out=run_rules(ctx,build_registry())
        bk=next(o for o in out.outcomes if o.rule.rule_id=="BK01")
        self.assertEqual(bk.to_result().industry_pack,"bank")

    def test_37_non_object_request_is_4xx(self):
        from app.main import app
        with TestClient(app,raise_server_exceptions=False) as client:
            for value in [[],None,42,"text"]:
                with self.subTest(value=value):
                    r=client.post("/api/scan",content=json.dumps(value),headers={"Content-Type":"application/json"})
                    self.assertLess(r.status_code,500)
                    self.assertGreaterEqual(r.status_code,400)

    def test_38_ambiguous_name_not_auto_selected(self):
        em=Mock()
        em.search.return_value=[{"Code":"600001","Name":"示例公司甲","Classify":"A"},
                                {"Code":"600002","Name":"示例公司乙","Classify":"A"}]
        em.resolve_suffix.return_value="SH";em.a_profile.return_value={}
        with IdentityResolver(em) as r:
            result=r.resolve("示例")
        self.assertFalse(result.ok)
        self.assertTrue(result.ambiguous)

    def test_39_noncompany_security_rejected(self):
        em=Mock();em.search.return_value=[{"Code":"510300","Name":"沪深300ETF","Classify":"Fund"}]
        em.resolve_suffix.return_value=None;em.a_profile.return_value={}
        with IdentityResolver(em) as r:result=r.resolve("510300")
        self.assertFalse(result.ok)

    def test_40_explicit_wrong_exchange_rejected(self):
        em=Mock();em.search.return_value=[];em.a_profile.return_value={"ORG_NAME":"深圳上市公司","SECUCODE":"000001.SZ"}
        with IdentityResolver(em) as r:result=r.resolve("000001.SH")
        self.assertFalse(result.ok)

    def _stub_pipeline(self,tid,deadline=None):
        pipe=ScanPipeline(tid,deadline_seconds=deadline)
        pipe.llm=LLMAdapter(LLMConfig(api_key=""))
        pipe._collect_documents=lambda *a:([],{"range":"fixture","total":0})
        pipe._build_evidence=lambda *a:(EvidenceStore(),{})
        pipe.em.a_statements=lambda *a:[fact(k,"2026-06-30",v,PeriodType.INTERIM) for k,v in
                                      {"revenue":100.,"net_profit":10.,"ocf":20.,"total_assets":200.}.items()]
        return pipe

    def _resolver(self):
        sec=Security("000001",Market.A,"测试公司","000001.SZ",org_name="测试公司",industry="制造业")
        return SimpleNamespace(ok=True,selected=sec,company=None,notes=[])

    def test_41_completed_task_not_partial_for_basis_note(self):
        pipe=self._stub_pipeline("clean")
        vals=[fact("revenue","2026-06-30",100.,PeriodType.INTERIM),
              fact("revenue","2025-06-30",90.,PeriodType.INTERIM),
              fact("net_profit","2026-06-30",10.,PeriodType.INTERIM),
              fact("ocf","2026-06-30",20.,PeriodType.INTERIM),
              fact("total_assets","2026-06-30",200.,PeriodType.INTERIM)]
        pipe.em.a_statements=lambda *a:vals
        # 隔离状态逻辑：所有适用规则均正常、无任何失败说明，只留下自动加入的口径说明。
        clean_output=run_rules(context(vals),build_registry())
        for o in clean_output.outcomes:
            if o.status!=RuleStatus.NOT_APPLICABLE:o.status=RuleStatus.NORMAL
        clean_output.coverage.evaluated=clean_output.coverage.applicable
        clean_output.coverage.insufficient=0
        with patch("app.engine.pipeline.IdentityResolver.resolve",return_value=self._resolver()),patch("app.engine.pipeline.run_rules",return_value=clean_output):
            result=pipe.run("test")
        self.assertEqual(len(result.payload["gaps"]),1)
        self.assertTrue(result.payload["gaps"][0].startswith("计算基准"))
        self.assertEqual(result.status,TaskStatus.SUCCEEDED,
                         "口径说明被放入 gaps，任何有财报扫描必然部分完成，缓存仅匹配完成")

    def test_42_expired_deadline_stops_expensive_steps(self):
        pipe=self._stub_pipeline("timeout",deadline=1)
        pipe.deadline=time.time()-1
        calls=[]
        pipe.em.a_statements=lambda *a:(calls.append("finance_after_deadline") or [])
        with patch("app.engine.pipeline.IdentityResolver.resolve",return_value=self._resolver()):pipe.run("test")
        self.assertEqual(calls,[])

    def test_43_five_parallel_pipelines_complete_without_db_lock(self):
        pipes=[self._stub_pipeline("concurrent"+str(i)) for i in range(5)]
        with patch("app.engine.pipeline.IdentityResolver.resolve",return_value=self._resolver()),ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda p:p.run("test"),pipes))
        self.assertTrue(all(r.payload and r.status!=TaskStatus.FAILED for r in results))
        self.assertTrue(all(len(db.load_rule_results(r.task_id))==52 for r in results))

    def test_44_db_rollback_works(self):
        try:
            with db.tx() as c:
                c.execute("INSERT INTO scan_tasks(task_id,query,status) VALUES('rollback','x','运行')")
                raise RuntimeError("fixture rollback")
        except RuntimeError:pass
        self.assertIsNone(db.get_task("rollback"))

    def test_45_ssrf_direct_loopback_rejected(self):
        with self.assertRaises(FetchError):assert_safe_url("http://127.0.0.1/")

    def test_46_partial_llm_batch_preserves_good_results(self):
        llm=LLMAdapter(LLMConfig(api_key="fixture",budget_cny=3.))
        with patch.object(llm,"chat_json",side_effect=[LLMResult(True,data=[{"rule_id":"one"}]),LLMResult(False,error="truncated")]):
            result=llm._run_batched([[1],[2]],lambda x:("s","u"),"test")
        self.assertTrue(result.ok);self.assertEqual(len(result.data),1);self.assertTrue(llm.failures)

    def test_47_unavailable_llm_never_calls_network(self):
        llm=LLMAdapter(LLMConfig(api_key=""))
        with patch("app.llm.adapter.httpx.Client") as transport:
            self.assertFalse(llm.chat_json("s","u").ok)
            transport.assert_not_called()

    def test_48_download_limit_is_enforced(self):
        c=HttpClient();c._client.close();c._client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b"x"*200)))
        try:
            with patch.object(settings,"max_file_bytes",100),patch("app.core.http_client.assert_safe_url"):
                with self.assertRaises(FetchError):c.download("https://example.org/file",self.tmp_path/"test.pdf")
            self.assertFalse((self.tmp_path/"test.pdf").exists());self.assertFalse((self.tmp_path/"test.pdf.part").exists())
        finally:c.close()

    def test_49_adjusted_debt_ratio_denominator(self):
        # 万科官网评级文件公式：(负债-预收-合同负债)/(资产-预收-合同负债)。
        # 无合同负债且预收为20的简例：(80-20)/(100-20)=75%，当前得到60%。
        vals=[fact(k,"2026-06-30",v) for k,v in
              {"total_assets":100.,"total_liabilities":80.,"advance_receivables":20.}.items()]
        o=rule("RE01").evaluate(context(vals,pack="realestate"))
        self.assertEqual(o.status,RuleStatus.RISK,o.finding)

    def test_50_duplicate_running_submissions_reused(self):
        from app import main
        with patch.object(main.executor,"submit"),TestClient(main.app) as client:
            a=client.post("/api/scan",json={"query":"duplicate"}).json()
            b=client.post("/api/scan",json={"query":"duplicate"}).json()
        with main._lock:
            main._running.pop(a["task_id"],None);main._running.pop(b["task_id"],None)
        self.assertEqual(a["task_id"],b["task_id"])

    def test_51_read_connection_is_closed(self):
        import sqlite3
        original=db.connect;seen=[]
        def track():
            c=original();seen.append(c);return c
        try:
            with patch.object(db,"connect",side_effect=track):db.get_task("missing")
            with self.assertRaises(sqlite3.ProgrammingError):seen[0].execute("SELECT 1")
        finally:
            for c in seen:c.close()

    def test_52_standard_unqualified_audit_not_flagged(self):
        p=ParsedDoc("normal",1,[(1,"会计师事务所对本年度财务报表出具了标准无保留意见。")],False)
        self.assertEqual(scan_audit_opinions(p),[])

    def test_53_full_rule_engine_routes_all_industry_packs(self):
        vals=[fact(k,"2026-06-30",v) for k,v in
              {"total_assets":100.,"total_liabilities":90.,"total_current_assets":10.,
               "total_current_liabilities":100.,"operating_profit":1.,"finance_expense":10.,
               "net_profit":-10.,"ocf":20.}.items()]
        for pack in ["general","bank","insurance","broker","realestate"]:
            with self.subTest(pack=pack):
                output=run_rules(context(vals,pack=pack),build_registry())
                self.assertEqual(len(output.outcomes),52)
                by_id={o.rule.rule_id:o for o in output.outcomes}
                self.assertEqual(by_id["FQ08"].status,RuleStatus.RISK)
                if pack in ["bank","insurance","broker"]:
                    for rid in ["SV02","SV03","SV04"]:
                        self.assertEqual(by_id[rid].status,RuleStatus.NOT_APPLICABLE)
                if pack=="realestate":self.assertEqual(by_id["SV02"].status,RuleStatus.NOT_APPLICABLE)


if __name__=="__main__":
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(AuditChecks)
    names=[t.id() for t in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    bad={}
    for kind,items in [("failure",result.failures),("error",result.errors)]:
        for test,trace in items:
            parent=getattr(test,"test_case",test)
            bad.setdefault(parent.id(),[]).append({"kind":kind,"case":str(test),"trace":trace})
    summary={"total_methods":result.testsRun,"passed_methods":len(names)-len(bad),
             "failed_methods":len(bad),"failure_assertions":len(result.failures),"errors":len(result.errors),
             "results":[{"test":n,"ok":n not in bad,"details":bad.get(n,[])} for n in names]}
    (Path(__file__).parent/"regression_results.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    sys.exit(0 if result.wasSuccessful() else 1)
