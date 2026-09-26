"""2026-09-26 项目审查：独立边界复现，合成数据，不访问网络或生产数据库。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_acceptance import context, fact, rule, doc
from app.config import settings, LLMConfig
from app.core import db
from app.core.models import PeriodType, RuleStatus as S, RiskEvent, TaskStatus
from app.data.cninfo import _classify
from app.data.hkexnews import _classify_hk, HkexnewsClient
from app.data.pdftext import ParsedDoc
from app.engine.normalize import FactSet, growth
from app.engine.metrics import compute_metrics
from app.engine import lifecycle
from app.llm.adapter import LLMAdapter, LLMResult
from app.report.charts import coverage_donut
from app.data.eastmoney import EastmoneyClient
from app.core.http_client import FetchError


class FinancialBoundaryTests(unittest.TestCase):
    def test_unverified_fact_cannot_clear_a_check(self):
        ctx = context([fact('ocf', '2025-12-31', 100, verified=False)])
        self.assertEqual(rule('FQ02').evaluate(ctx).status, S.INSUFFICIENT)

    def test_nonfinite_facts_are_not_indexed(self):
        for value in [float('nan'), float('inf'), float('-inf')]:
            with self.subTest(value=value):
                fs = FactSet([fact('ocf', '2025-12-31', value)])
                self.assertIsNone(fs.get('ocf', '2025-12-31'))

    def test_invalid_period_does_not_crash_metrics(self):
        for period in ['2025-13-31', 'not-a-date']:
            with self.subTest(period=period):
                self.assertEqual(compute_metrics(FactSet([fact('ocf', period, 1)])).latest_period, '')

    def test_growth_does_not_produce_nonfinite_result(self):
        for a, b in [(float('nan'), 1), (1, float('inf')), (1e308, 1e-5)]:
            self.assertIsNone(growth(a, b))

    def test_series_does_not_mix_interim_and_annual(self):
        fs = FactSet([fact('ocf', '2025-12-31', 100),
                      fact('ocf', '2026-06-30', 20, PeriodType.INTERIM),
                      fact('ocf', '2025-06-30', 15, PeriodType.INTERIM)])
        self.assertEqual([p.period_end for p in fs.series('ocf')], ['2026-06-30', '2025-06-30'])

    def test_series_does_not_mix_currencies(self):
        fs = FactSet([fact('ocf', '2025-12-31', 100, currency='CNY'),
                      fact('ocf', '2024-12-31', 90, currency='USD')])
        self.assertEqual(len(fs.series('ocf')), 1)

    def test_unknown_currency_not_plotted_as_money(self):
        self.assertEqual(FactSet([fact('ocf', '2025-12-31', 100, currency='未核实')]).series('ocf'), [])

    def test_negative_profit_not_cleared_by_nonrecurring_ratio(self):
        ctx = context([fact('net_profit_attributable', '2025-12-31', -100),
                       fact('net_profit_deducted', '2025-12-31', -200)])
        self.assertEqual(rule('FQ06').evaluate(ctx).status, S.NOT_APPLICABLE)

    def test_nonrecurring_does_not_mix_total_and_attributable_profit(self):
        ctx = context([fact('net_profit', '2025-12-31', 100),
                       fact('net_profit_deducted', '2025-12-31', 40)])
        self.assertIsNone(ctx.metrics.get('nonrecurring_ratio'))

    def test_nonrecurring_requires_matching_period_start(self):
        ctx = context([fact('net_profit_attributable', '2025-12-31', 100, period_start='2025-01-01'),
                       fact('net_profit_deducted', '2025-12-31', 40, period_start='2025-10-01')])
        self.assertIsNone(ctx.metrics.get('nonrecurring_ratio'))

    def test_missing_profit_not_treated_as_reported_profit(self):
        ctx = context([fact('net_profit_deducted', '2025-12-31', -10)])
        self.assertEqual(rule('FQ07').evaluate(ctx).status, S.WATCH)

    def test_negative_equity_detected_without_cashflow(self):
        ctx = context([fact('total_equity', '2025-12-31', -10)])
        self.assertEqual(rule('OP05').evaluate(ctx).status, S.RISK)

    def test_audit_going_concern_detected_without_financial_data(self):
        d = doc('年度报告', '年报')
        p = ParsedDoc(d.doc_id, 1, [(1, '我们提醒财务报表使用者关注：存在可能导致对本公司持续经营能力产生重大疑虑的重大不确定性。')], False)
        ctx = context(docs=[d], parsed={d.doc_id:p})
        self.assertEqual(rule('OP05').evaluate(ctx).status, S.WATCH)

    def test_missing_cashflow_and_positive_profit_not_normal(self):
        self.assertEqual(rule('OP05').evaluate(context([fact('net_profit', '2025-12-31', 100)])).status, S.INSUFFICIENT)

    def test_bank_source_indicator_reaches_bank_rule(self):
        ctx=context([fact('bank_loan_deposit_ratio','2025-12-31',.95,unit='比率')],pack='bank')
        self.assertEqual(rule('BK01').evaluate(ctx).status,S.WATCH)

    def test_zero_short_borrowings_not_missing_data(self):
        for pack,rid in [('general','SV01'),('realestate','RE02')]:
            ctx=context([fact('short_term_borrowings','2025-12-31',0)],pack=pack)
            self.assertEqual(rule(rid).evaluate(ctx).status,S.NOT_APPLICABLE)

    def test_net_finance_income_not_proof_of_no_interest_pressure(self):
        for expense in [-10,0]:
            ctx=context([fact('operating_profit','2025-12-31',100),fact('finance_expense','2025-12-31',expense)])
            self.assertEqual(rule('SV04').evaluate(ctx).status,S.NOT_APPLICABLE)

    def test_invalid_cash_equivalents_cannot_clear_debt_check(self):
        ctx=context([fact('cash_equivalents','2025-12-31',-100),fact('short_term_borrowings','2025-12-31',10)])
        self.assertIsNone(ctx.metrics.get('usable_cash'))

    def test_cash_cannot_be_less_than_restricted_cash(self):
        ctx=context([fact('cash','2025-12-31',100),fact('restricted_cash','2025-12-31',120),
                     fact('cash_equivalents','2025-12-31',200)])
        self.assertIsNone(ctx.metrics.get('usable_cash'))


class DisclosureBoundaryTests(unittest.TestCase):
    def test_a_share_risk_title_wins_over_report_carrier(self):
        self.assertEqual(_classify('关于年度报告涉及行政处罚事项的公告'), '监管处罚')

    def test_hk_risk_title_wins_over_results_carrier(self):
        self.assertEqual(_classify_hk('年度業績暨訴訟進展公告', ''), '诉讼')

    def test_hk_interim_not_annual(self):
        self.assertEqual(_classify_hk('半年度報告', ''), '半年报')

    def test_hk_simplified_risk_title(self):
        self.assertEqual(_classify_hk('关于资产冻结的公告', ''), '资产冻结')

    def test_hk_single_day_overflow_is_visible(self):
        client = HkexnewsClient(Mock())
        from datetime import date
        rows = [{'NEWS_ID':str(i), 'FILE_LINK':f'/a{i}.pdf', 'TITLE':'公告', 'DATE_TIME':'01/01/2026'} for i in range(2)]
        with patch.object(client, '_query_window', return_value=(rows, 3, '')):
            result = client.announcements('00700', '1', date(2026,1,1), date(2026,1,1))
        self.assertTrue(any('无法再拆分' in g for g in result['gaps']))

    def test_hk_two_day_overflow_splits_disjoint_windows(self):
        from datetime import date
        client = HkexnewsClient(Mock())
        first, second = date(2026, 1, 1), date(2026, 1, 2)
        def row(identifier, day):
            return {'NEWS_ID': identifier, 'FILE_LINK': f'/{identifier}.pdf',
                    'TITLE': '公告', 'DATE_TIME': day}
        with patch.object(client, '_query_window', side_effect=[
            ([row('a', '01/01/2026')], 2, ''),
            ([row('a', '01/01/2026')], 1, ''),
            ([row('b', '02/01/2026')], 1, ''),
        ]) as query:
            result = client.announcements('00700', '1', first, second)
        self.assertEqual([call.args[1:] for call in query.call_args_list],
                         [(first, second), (first, first), (second, second)])
        self.assertEqual(result['fetched'], 2)
        self.assertEqual(result['gaps'], [])

    def test_reply_does_not_resolve_inquiry(self):
        event = RiskEvent('e', '问询函（公告编号：2026-001）', '2026-01-01', '监管问询', '摘要')
        d = doc('问询函回复（公告编号：2026-001）', '监管问询')
        self.assertFalse(lifecycle.detect_resolution(event, [d])[0])

    def test_judgment_does_not_resolve_lawsuit_obligation(self):
        event = RiskEvent('e', '(2026)粤0304民初123号诉讼公告', '2026-01-01', '诉讼', '摘要')
        d = doc('(2026)粤0304民初123号诉讼判决公告', '诉讼')
        self.assertFalse(lifecycle.detect_resolution(event, [d])[0])

    def test_hypothetical_release_title_not_resolved(self):
        event = RiskEvent('e', '(2026)粤0304民初123号资产冻结', '2026-01-01', '资产冻结', '摘要')
        d = doc('(2026)粤0304民初123号拟申请解除冻结', '冻结解除')
        self.assertFalse(lifecycle.detect_resolution(event, [d])[0])

    def test_model_claim_without_resolution_basis_not_retained(self):
        event = RiskEvent('e', '资产冻结公告', '2026-01-01', '资产冻结', '摘要', resolved=True)
        self.assertIsNot(lifecycle.enrich_events([event], [])[0].resolved, True)

    def test_recurrence_not_cleared_by_old_resolution(self):
        events = [RiskEvent('e1','资产冻结','2026-01-01','资产冻结','摘要', resolved=True, resolution_basis='解除冻结', resolution_date='2026-02-01'),
                  RiskEvent('e2','资产冻结','2026-03-01','资产冻结','摘要')]
        self.assertFalse(lifecycle.build_lifecycles(events)[0]['resolved'])


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pailei-project-audit-')
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(settings, 'db_path', self.root/'app.db')]
        for p in self.patches:p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_task_constructor_failure_cleans_running_slot(self):
        import app.main as web
        task_id='constructor_failure'
        db.create_task(task_id, '600519')
        web._running[task_id]=time.time()
        try:
            with patch.object(web, 'ScanPipeline', side_effect=RuntimeError('constructor failed')):
                web._run_task(task_id, '600519')
            self.assertEqual(db.get_task(task_id)['status'], TaskStatus.FAILED.value)
            self.assertNotIn(task_id, web._running)
        finally:
            web._running.pop(task_id, None)

    def test_result_reuse_requires_a_saved_report(self):
        db.create_task('no_report', '600519')
        db.update_task('no_report', status=TaskStatus.SUCCEEDED.value)
        self.assertIsNone(db.find_recent_task('600519'))

    def test_full_coverage_donut_is_100_percent(self):
        self.assertIn('检查覆盖度 100%', coverage_donut(40,40,0))

    def test_partial_coverage_donut_matches_fraction(self):
        self.assertIn('检查覆盖度 75%', coverage_donut(40,30,10))

    def test_zero_coverage_donut(self):
        self.assertIn('检查覆盖度 0%', coverage_donut(0,0,0))

    def test_business_error_is_not_a_successful_empty_financial_response(self):
        client=Mock()
        client.get_json.return_value={'success':False,'message':'invalid report','result':None}
        with self.assertRaises(FetchError):
            EastmoneyClient(client).query_all('broken', '600519.SH', stage='finance')
        self.assertEqual(len(list((self.root/'raw/eastmoney').glob('*.json'))), 1)

    def test_malformed_financial_response_is_reported(self):
        client=Mock()
        client.get_json.return_value={'success':True,'result':{'pages':1,'data':'not rows'}}
        with self.assertRaises(FetchError):
            EastmoneyClient(client).query_all('broken', '600519.SH', stage='finance')

    def test_missing_llm_usage_reserves_estimated_budget(self):
        llm=LLMAdapter(LLMConfig(api_key='fixture',budget_cny=10,price_in_cny_per_1m=10,price_out_cny_per_1m=10))
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'message':{'content':'[]'},'finish_reason':'stop'}]}
        client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        client.request.return_value=response
        with patch('app.llm.adapter.HttpClient',return_value=client):
            result=llm.chat_json('s','u')
        self.assertTrue(result.ok)
        self.assertGreater(llm.spent_cny,0)
        self.assertTrue(any('用量' in f for f in llm.failures))

    def test_cached_batch_after_an_overbudget_miss_is_still_used(self):
        from app.llm import cache
        from app.llm.adapter import PROMPT_VERSION, VALIDATION_VERSION
        llm=LLMAdapter(LLMConfig(api_key='fixture',budget_cny=1,price_in_cny_per_1m=1000000))
        llm.spent_cny=1
        key=cache.compute_cache_key(llm.config,'s','[2]',step='test',prompt_version=PROMPT_VERSION,validation_version=VALIDATION_VERSION)
        cache.put(key,data=[{'id':'2'}],input_tokens=1,output_tokens=1,cost_cny=1,validation_version=VALIDATION_VERSION)
        with patch.object(llm,'_chat_json_uncached',side_effect=AssertionError('must use cache')):
            result=llm._run_batched([[1],[2]],lambda b:('s',str(b)),'test')
        self.assertTrue(result.ok)
        self.assertEqual(result.data,[{'id':'2'}])

    def test_unconfirmed_risk_not_labelled_highest_confirmed_risk(self):
        from app.engine.pipeline import ScanPipeline
        from app.engine.runner import run_rules, build_registry
        ctx=context([fact('ocf','2025-12-31',-100),fact('net_profit','2025-12-31',100)])
        output=run_rules(ctx,build_registry())
        pipeline=ScanPipeline.__new__(ScanPipeline)
        pipeline.task_id='fixture';pipeline.gaps=[];pipeline.notes=[]
        pipeline.llm=LLMAdapter(LLMConfig(enabled=False))
        payload=pipeline._build_payload(security=ctx.security,facts=ctx.facts,metrics=ctx.metrics,
            output=output,docs=[],evidence_store=ctx.evidence,events=[],company=None,
            announcement_meta={},industry_pack='general',plan={},started=time.time(),
            elapsed=1,timed_out=False,verification_count=0,network={})
        self.assertEqual(payload['summary']['highest_severity'], '未定')
        self.assertGreater(payload['summary']['risk_count'],0)

    def test_progress_page_can_display_newly_completed_coverage(self):
        from app import main
        from fastapi.testclient import TestClient
        db.create_task('queued','600519')
        with TestClient(main.app) as client:
            html=client.get('/scan/queued').text
        self.assertIn('id="cov-badge"',html)

    def test_old_buggy_rule_version_is_not_reused(self):
        from app import main
        from fastapi.testclient import TestClient
        db.create_task('old','600519')
        db.update_task('old',status=TaskStatus.SUCCEEDED.value,rule_version='1.3')
        db.save_report('old','','',{'task_id':'old','rule_version':'1.3'})
        task_id=None
        try:
            with patch.object(main.executor,'submit'), TestClient(main.app) as client:
                response=client.post('/api/scan',json={'query':'600519'}).json()
            task_id=response['task_id']
            self.assertFalse(response['reused'])
        finally:
            if task_id:
                with main._lock: main._running.pop(task_id,None)

    def test_dedup_link_failure_preserves_original(self):
        from app.core.storage import dedup
        files=self.root/'files';files.mkdir()
        original=files/'original.pdf';original.write_bytes(b'original')
        blob=files/'_blob/hash.pdf';blob.parent.mkdir();blob.write_bytes(b'original')
        with patch.object(settings,'files_dir',files),patch('app.core.storage.os.link',side_effect=OSError('not supported')):
            returned=dedup(original,'hash')
        self.assertEqual(returned,original)
        self.assertEqual(original.read_bytes(),b'original')

    def test_interrupted_parser_temp_file_is_cleaned(self):
        from app.core.download_cleanup import cleanup_downloads
        cache=self.root/'cache/pdf_parse';cache.mkdir(parents=True)
        tmp=cache/('a'*64+'.1.120.0.json.random.tmp');tmp.write_text('partial')
        with patch.object(settings,'cache_dir',self.root/'cache'),patch.object(settings,'files_dir',self.root/'files'):
            result=cleanup_downloads()
        self.assertEqual(result['removed_files'],1)
        self.assertFalse(tmp.exists())

    def test_cached_batch_can_run_after_budget_is_spent(self):
        from app.llm import cache
        from app.llm.adapter import PROMPT_VERSION, VALIDATION_VERSION
        llm = LLMAdapter(LLMConfig(api_key='fixture', budget_cny=1, price_in_cny_per_1m=1000000))
        llm.spent_cny=1
        key=cache.compute_cache_key(llm.config,'s','u',step='test',prompt_version=PROMPT_VERSION,validation_version=VALIDATION_VERSION)
        cache.put(key,data=[{'rule_id':'FQ01'}],input_tokens=1,output_tokens=1,cost_cny=1,validation_version=VALIDATION_VERSION)
        with patch.object(llm, '_chat_json_uncached', side_effect=AssertionError('must use cache')):
            result=llm._run_batched([[1]], lambda b:('s','u'), 'test')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data), 1)


if __name__ == '__main__':
    unittest.main()
