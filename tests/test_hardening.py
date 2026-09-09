"""修复后的独立回归：真实接口形状、正反边界与完整输出契约。全部离线。"""
import copy
import json
import socket
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx
import httpcore
from fastapi.testclient import TestClient
from jinja2 import UndefinedError
from test_acceptance import context, fact, rule, doc, baseline
from app.config import settings, LLMConfig
from app.core import db
from app.core.http_client import HttpClient, FetchError, FetchRecord, PublicNetworkBackend
from app.core.models import *
from app.data.identity import IdentityResolver, _sec_market
from app.data.cninfo import CninfoClient
from app.data.eastmoney import _period_start, _hk_rows_to_facts, HK_BALANCE_MAP, _num
from app.data.pdftext import ParsedDoc, parse_pdf, verify_evidence
from app.engine.currency import verify_reporting_currencies
from app.engine.metrics import compute_metrics
from app.engine.normalize import FactSet
from app.engine.runner import run_rules, build_registry
from app.engine.rules.base import EvidenceStore, RULE_VERSION
from app.llm.adapter import LLMAdapter, LLMResult
from app.report.render import render_inline, render_report


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pailei-fix-')
        self.root = Path(self.temp.name)
        self.patches = [patch.object(settings,'db_path',self.root/'app.db'),
                        patch.object(settings,'reports_dir',self.root/'reports'),
                        patch.object(settings,'files_dir',self.root/'files')]
        for p in self.patches:p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_real_astock_shape_and_index_filtered(self):
        em=Mock();em.search.return_value=[{'Code':'000001','Name':'平安银行','Classify':'AStock'},
                                         {'Code':'000001','Name':'上证指数','Classify':'Index'}]
        em.resolve_suffix.return_value='SZ';em.a_profile.return_value={'SECUCODE':'000001.SZ','ORG_NAME':'平安银行','CSRC_INDUSTRY_NAME':'银行'}
        with IdentityResolver(em) as r:
            result=r.resolve('000001.SZ')
        self.assertTrue(result.ok);self.assertEqual(result.selected.name,'平安银行')

    def test_star_market_classify_23_recognized(self):
        # 科创板股票的 Classify 字段是交易所代码 "23" 而非 "AStock"，曾导致被过滤
        em=Mock()
        em.search.return_value=[{'Code':'688981','Name':'中芯国际','Classify':'23','SecurityTypeName':'科创板'}]
        em.resolve_suffix.return_value='SH'
        em.a_profile.return_value={'SECUCODE':'688981.SH','ORG_NAME':'中芯国际集成电路制造有限公司','CSRC_INDUSTRY_NAME':'制造业'}
        with IdentityResolver(em) as r:
            result=r.resolve('688981')
        self.assertTrue(result.ok)
        self.assertEqual(result.selected.secucode,'688981.SH')
        self.assertEqual(result.selected.exchange,'上海证券交易所')

    def test_bse_classify_neeq_recognized(self):
        # 北交所股票的 Classify 字段是 "NEEQ" 而非 "AStock"，曾导致被过滤
        em=Mock()
        em.search.return_value=[{'Code':'920799','Name':'艾融软件','Classify':'NEEQ','SecurityTypeName':'京A'}]
        em.resolve_suffix.return_value='BJ'
        em.a_profile.return_value={'SECUCODE':'920799.BJ','ORG_NAME':'上海艾融软件股份有限公司','CSRC_INDUSTRY_NAME':'软件和信息技术服务业'}
        with IdentityResolver(em) as r:
            result=r.resolve('920799')
        self.assertTrue(result.ok)
        self.assertEqual(result.selected.secucode,'920799.BJ')
        self.assertEqual(result.selected.exchange,'北京证券交易所')

    def test_cninfo_resolve_org_bse_prefix(self):
        # 北交所 orgId 前缀为 gfbj，须映射为 column=bj（曾兜底成 szse 导致公告取不到）
        cases=[
            ([{'code':'920799','orgId':'gfbj0830799'}], '920799', ('gfbj0830799','bj')),
            ([{'code':'600519','orgId':'gssh0600519'}], '600519', ('gssh0600519','sse')),
            ([{'code':'000002','orgId':'gssz0000002'}], '000002', ('gssz0000002','szse')),
        ]
        for hits, code, expected in cases:
            with self.subTest(code=code):
                with patch.object(CninfoClient, 'search', return_value=hits):
                    c=CninfoClient()
                    try:
                        self.assertEqual(c.resolve_org(code), expected)
                    finally:
                        c.close()

    def test_sec_market_classifies_all_boards(self):
        cases=[
            ({'Classify':'AStock','SecurityTypeName':'沪A'}, Market.A),
            ({'Classify':'AStock','SecurityTypeName':'深A'}, Market.A),
            ({'Classify':'23','SecurityTypeName':'科创板'}, Market.A),
            ({'Classify':'NEEQ','SecurityTypeName':'京A'}, Market.A),
            ({'Classify':'HK','SecurityTypeName':'港股'}, Market.HK),
            ({'Classify':'Index','SecurityTypeName':'指数'}, None),
            ({'Classify':'OTCFUND','SecurityTypeName':'基金'}, None),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(_sec_market(item), expected)

    def test_single_fuzzy_candidate_requires_confirmation(self):
        em=Mock();em.search.return_value=[{'Code':'600519','Name':'贵州茅台','Classify':'AStock'}];em.resolve_suffix.return_value='SH'
        with IdentityResolver(em) as r: result=r.resolve('茅')
        self.assertTrue(result.ambiguous);self.assertFalse(result.ok)

    def test_cash_equivalents_not_reduced_twice(self):
        rows=[{'REPORT_DATE':'2026-06-30','DATE_TYPE_CODE':'002','STD_ITEM_CODE':'004002010','STD_ITEM_NAME':'現金及現金等價物','AMOUNT':206930e6},
              {'REPORT_DATE':'2026-06-30','DATE_TYPE_CODE':'002','STD_ITEM_CODE':'004002009','STD_ITEM_NAME':'受限制現金','AMOUNT':7729e6}]
        fs=FactSet(_hk_rows_to_facts(rows,'00700.HK',Statement.BALANCE,HK_BALANCE_MAP,'fixture','CNY'))
        m=compute_metrics(fs,market=Market.HK)
        self.assertEqual(m.get('usable_cash'),206930e6)
        self.assertEqual(m.get('cash'),214659e6)

    def test_currency_requires_financial_values_not_trading_counter(self):
        vals=[fact('revenue','2026-06-30',401243e6,currency='未核实'),fact('net_profit','2026-06-30',114115e6,currency='未核实')]
        d=doc('中期報告2026','半年报');p=ParsedDoc(d.doc_id,1,[(1,'港幣櫃台 700 人民幣櫃台80700\n人民幣百萬元\n收入 401,243\n期內盈利 114,115')],False)
        self.assertEqual(verify_reporting_currencies(vals,[d],{d.doc_id:p}),[])
        self.assertEqual({f.currency for f in vals},{'CNY'})

    def test_currency_unverified_blocks_monetary_ratios(self):
        vals=[fact('revenue','2026-06-30',100.,currency='HKD')]
        self.assertTrue(verify_reporting_currencies(vals,[],{}))
        self.assertIsNone(compute_metrics(FactSet(vals)).get('revenue'))

    def test_currency_not_copied_to_unmatched_year(self):
        vals=[fact('revenue','2025-06-30',100e6,currency='未核实'),fact('net_profit','2025-06-30',50e6,currency='未核实')]
        d=doc('中期報告2026','半年报');p=ParsedDoc(d.doc_id,1,[(1,'2026 人民幣百萬元 收入100 淨利潤50')],False)
        self.assertTrue(verify_reporting_currencies(vals,[d],{d.doc_id:p}))
        self.assertEqual(vals[0].currency,'未核实')

    def test_period_start_noncalendar_and_q1(self):
        self.assertEqual(_period_start('2026-03-31',PeriodType.Q1),'2026-01-01')
        self.assertEqual(_period_start('2026-03-31',PeriodType.ANNUAL),'2025-04-01')
        self.assertEqual(_period_start('2025-09-30',PeriodType.INTERIM),'2025-04-01')

    def test_latest_restatement_not_input_order(self):
        old=fact('revenue','2026-06-30',100.,notice_date='2026-07-01')
        revised=fact('revenue','2026-06-30',90.,notice_date='2026-08-01')
        for vals in ([old,revised],[revised,old]):
            fs=FactSet(vals);self.assertEqual(fs.value('revenue','2026-06-30'),90.);self.assertEqual(fs.latest('revenue').value,90.)

    def test_zero_equity_not_replaced_by_alternate(self):
        m=compute_metrics(FactSet([fact('total_equity','2026-06-30',0.),fact('net_assets','2026-06-30',100.)]))
        self.assertEqual(m.get('total_equity'),0.)

    def test_financial_nonfinite_rejected(self):
        for x in ['nan',float('inf'),'-inf']:
            self.assertIsNone(_num(x))

    def test_full_restricted_zero_rules_risk(self):
        ctx=context([fact(k,'2026-06-30',v) for k,v in {'cash':100.,'restricted_cash':100.,'short_term_borrowings':50.}.items()])
        self.assertEqual(rule('SV01').evaluate(ctx).status,RuleStatus.RISK)

    def test_all_profit_cash_sign_combinations(self):
        for profit,ocf,expected in [(100,-100,RuleStatus.RISK),(100,20,RuleStatus.WATCH),(100,100,RuleStatus.NORMAL),(-100,100,RuleStatus.NOT_APPLICABLE),(-100,-100,RuleStatus.NOT_APPLICABLE),(0,0,RuleStatus.INSUFFICIENT)]:
            with self.subTest(profit=profit,ocf=ocf):
                ctx=context([fact('net_profit','2026-06-30',profit),fact('ocf','2026-06-30',ocf)])
                self.assertEqual(rule('FQ01').evaluate(ctx).status,expected)

    def test_contract_liabilities_missing_is_not_zero(self):
        ctx=context([fact(k,'2026-06-30',v) for k,v in {'total_assets':100.,'total_liabilities':80.,'advance_receivables':20.}.items()],pack='realestate')
        self.assertEqual(rule('RE01').evaluate(ctx).status,RuleStatus.INSUFFICIENT)

    def test_evidence_document_and_tail_and_page_must_match(self):
        p=ParsedDoc('real',2,[(1,'真实句子完整内容'),(2,'别的内容')],False)
        for docid,quote,location in [('other','真实句子完整内容','第 1 页'),('real','真实句子完整内容伪造尾巴','第 1 页'),('real','真实句子完整内容','第 2 页')]:
            e=Evidence('e',docid,'报告',quote,location=location,fingerprint=Evidence.fingerprint_of(quote),verified=True)
            self.assertFalse(verify_evidence(e,p).verified)

    def test_audit_signal_full_rule_evidence_result(self):
        d=doc(kind='审计');text='由於重大事項，我們無法表示意見。'
        ctx=context(docs=[d],parsed={d.doc_id:ParsedDoc(d.doc_id,1,[(1,text)],False)})
        out=run_rules(ctx,build_registry());o=next(o for o in out.outcomes if o.rule.rule_id=='OP02')
        self.assertEqual(o.status,RuleStatus.RISK);self.assertTrue(o.evidence_ids)
        self.assertEqual(o.strength,EvidenceStrength.CONFIRMED)
        self.assertIn('無法表示意見',ctx.evidence.get(o.evidence_ids[0]).quote)
        self.assertTrue(ctx.evidence.get(o.evidence_ids[0]).verified)

    def test_numeric_evidence_does_not_select_address_page(self):
        d=doc('2026年半年报','半年报');p=ParsedDoc(d.doc_id,2,[(1,'公司地址，应收账款业务简介'),(2,'2026年 单位元\n应收账款 100.00\n收入 100.00')],False)
        vals=[fact('accounts_receivable','2026-06-30',100.),fact('revenue','2026-06-30',100.)]
        ctx=context(vals,docs=[d],parsed={d.doc_id:p});out=run_rules(ctx,build_registry());o=next(o for o in out.outcomes if o.rule.rule_id=='FQ04')
        self.assertTrue(o.evidence_ids);self.assertTrue(all(ctx.evidence.get(eid).location=='第 2 页' for eid in o.evidence_ids))
        self.assertNotEqual(o.strength,EvidenceStrength.CONFIRMED)

    def test_pdf_hard_timeout_terminates_worker(self):
        p=self.root/'empty.pdf';p.write_bytes(b'%PDF-fixture')
        start=time.monotonic();parsed=parse_pdf(p,timeout=.001)
        self.assertIn('超时',parsed.error);self.assertLess(time.monotonic()-start,1.)

    def test_download_redirect_private_not_contacted(self):
        visited=[]
        def respond(r):
            visited.append(r.url.host);return httpx.Response(302,headers={'Location':'http://127.0.0.1/private'})
        c=HttpClient();c._client.close();c._client=httpx.Client(transport=httpx.MockTransport(respond))
        try:
            with patch('app.core.http_client.socket.getaddrinfo',return_value=[(socket.AF_INET,1,6,'',('93.184.216.34',80))]):
                with self.assertRaises(FetchError):c.download('https://public.test/file',self.root/'file.pdf')
            self.assertEqual(visited,['public.test'])
        finally:c.close()

    def test_connection_uses_verified_ip_not_second_dns_lookup(self):
        with patch('app.core.http_client.socket.getaddrinfo',return_value=[(socket.AF_INET,1,6,'',('93.184.216.34',80))]),patch.object(httpcore.SyncBackend,'connect_tcp',return_value=Mock()) as connect:
            PublicNetworkBackend().connect_tcp('public.test',443,timeout=1)
        self.assertEqual(connect.call_args.args[0],'93.184.216.34')

    def test_deadline_prevents_transport(self):
        with HttpClient() as c:
            c.deadline=time.time()-1
            with patch.object(c._client,'send') as send:
                with self.assertRaises(FetchError):c.request('GET','https://example.org')
                send.assert_not_called()

    def test_direct_llm_budget_holds_before_request(self):
        llm=LLMAdapter(LLMConfig(api_key='fixture',budget_cny=.01,max_output_tokens=10000,price_out_cny_per_1m=10))
        with patch('app.llm.adapter.HttpClient') as client:
            r=llm.chat_json('system','user')
        client.assert_not_called();self.assertFalse(r.ok);self.assertIn('预算',r.error)

    def test_llm_empty_events_is_valid_success(self):
        llm=LLMAdapter(LLMConfig(api_key='fixture'))
        with patch.object(llm,'chat_json',return_value=LLMResult(True,data=[])) as chat:
            r=llm.extract_events([{'doc_id':'d','title':'例行公告'}])
        self.assertTrue(r.ok);self.assertEqual(r.data,[]);self.assertFalse(llm.failures)
        self.assertEqual(chat.call_args.kwargs['max_tokens'],8000)

    def test_llm_network_switch_disables_all_steps(self):
        with patch.object(settings,'enable_network',False),patch('app.llm.adapter.HttpClient') as client:
            llm=LLMAdapter(LLMConfig(api_key='fixture'));self.assertFalse(llm.chat_json('s','u').ok)
        client.assert_not_called()

    def test_database_migration_preserves_existing_rows(self):
        path=self.root/'legacy.db'
        with closing(sqlite3.connect(path)) as c:
            c.executescript(db.SCHEMA)
            c.execute("INSERT INTO scan_tasks(task_id,query,status) VALUES('legacy','000001','完成')")
            c.commit()
        with patch.object(settings,'db_path',path):
            db.init_db();db.init_db();self.assertEqual(db.get_task('legacy')['status'],'完成')
            f=fact('revenue','2026-06-30',100.,period_start='2026-01-01',audited=False,consolidated=True)
            db.save_facts('legacy',[f]);row=db.load_facts('legacy')[0]
            self.assertEqual((row['period_start'],row['audited'],row['consolidated']),('2026-01-01',0,1))

    def test_identical_real_requests_each_count_once(self):
        logs=[FetchRecord('https://example.org','test',True,host='example.org') for _ in range(2)]
        db.save_fetch_logs('t',logs);db.save_fetch_logs('t',logs)
        self.assertEqual(db.source_stats()[0]['ok_count'],2)

    def test_online_download_and_saved_html_equal(self):
        from app.main import app
        p=baseline.TestReportRendering._payload();p['generated_at']='2026-09-06T12:00:00'
        html,jsonpath=render_report(p);db.save_report(p['task_id'],html,jsonpath,p)
        with TestClient(app) as c:
            online=c.get('/report/'+p['task_id']);download=c.get('/download/'+p['task_id'])
        self.assertEqual(online.content,download.content);self.assertEqual(online.content,Path(html).read_bytes())
        self.assertIn('2026-09-06T12:00:00',online.text)

    def test_strict_template_rejects_missing_required_field(self):
        p=baseline.TestReportRendering._payload();p['security'].pop('secucode')
        with self.assertRaises(UndefinedError):render_inline(p)

    def test_bad_field_types_and_length_return_4xx(self):
        from app.main import app
        with TestClient(app) as c:
            for body in [{'query':['600519']},{'query':'600519','force':'false'},{'query':'x'*121}]:
                self.assertEqual(c.post('/api/scan',json=body).status_code,400)

    def test_queue_limit_rejects_before_task_creation(self):
        from app import main
        with main._lock:original=dict(main._running);main._running.update({f'busy{i}':time.time() for i in range(main.MAX_WORKERS*2)})
        try:
            with TestClient(main.app) as c:self.assertEqual(c.post('/api/scan',json={'query':'600519'}).status_code,429)
        finally:
            with main._lock:main._running.clear();main._running.update(original)

    def test_partial_result_cache_reused(self):
        from app.main import app
        db.create_task('partial','600519');db.update_task('partial',status=TaskStatus.SUCCEEDED.value,coverage_level='一般缺口',rule_version=RULE_VERSION)
        with TestClient(app) as c:r=c.post('/api/scan',json={'query':'600519'}).json()
        self.assertEqual(r['task_id'],'partial');self.assertTrue(r['reused'])

    def test_slow_dns_wait_is_bounded(self):
        from app.core.http_client import assert_safe_url
        def slow(*args):
            time.sleep(.1)
            return [(socket.AF_INET,1,6,'',('93.184.216.34',80))]
        started=time.monotonic()
        with patch('app.core.http_client.socket.getaddrinfo',side_effect=slow):
            with self.assertRaises(FetchError):assert_safe_url('https://public.test',timeout=.01)
        self.assertLess(time.monotonic()-started,.08)

    def test_multicast_addresses_rejected(self):
        from app.core.http_client import assert_safe_url
        for url in ['http://224.0.0.1/','http://[ff02::1]/']:
            with self.assertRaises(FetchError):assert_safe_url(url)

    def test_old_goodwill_not_divided_by_current_assets(self):
        ctx=context([fact('goodwill','2025-06-30',20.),fact('total_assets','2026-06-30',100.)])
        self.assertEqual(rule('OP04').evaluate(ctx).status,RuleStatus.INSUFFICIENT)

    def test_model_verification_invalid_shape_is_reported(self):
        llm=LLMAdapter(LLMConfig(api_key='fixture'))
        with patch.object(llm,'chat_json',return_value=LLMResult(True,data=42)):
            r=llm.verify({'items':[{'rule_id':'FQ08'}]})
        self.assertFalse(r.ok);self.assertTrue(llm.failures)

    def test_stale_rule_version_cache_is_not_reused(self):
        db.create_task('old','600519');db.update_task('old',status=TaskStatus.SUCCEEDED.value,rule_version='1.0')
        self.assertIsNone(db.find_recent_task('600519',rule_version='1.1'))

    def test_a_indicator_without_type_is_not_annual(self):
        from app.data.eastmoney import EastmoneyClient
        client=EastmoneyClient(Mock())
        with patch.object(client,'query_all',return_value=[{'REPORT_DATE':'2026-06-30','EPSJB':1.0}]):
            facts=client._a_indicators('600519.SH')
        self.assertEqual(facts[0].period_type,PeriodType.INTERIM)
        self.assertEqual(facts[0].period_start,'2026-01-01')
        self.assertEqual(FactSet(facts).periods(PeriodType.ANNUAL),[])


if __name__=='__main__':unittest.main(verbosity=2)
