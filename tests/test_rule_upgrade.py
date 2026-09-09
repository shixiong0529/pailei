"""V1.3 behavioral regression: positive/negative disclosure, numeric boundary, lineage, old report compatibility."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from test_acceptance import context, doc, fact, rule
from app.core.models import Market, PeriodType, RuleStatus as S, Severity, Statement
from app.data.pdftext import ParsedDoc
from app.engine.rules.extended import text_check
from app.engine.runner import run_rules, build_registry
from app.report.render import risk_signal_score, render_inline
from app.config import settings


def text_context(text, kind='年报', title='年度报告', pack='general'):
    d = doc(title, kind)
    return context(docs=[d], parsed={d.doc_id: ParsedDoc(d.doc_id, 1, [(1, text)], False)}, pack=pack)


def evaluate_text(rid, text, **kw):
    ctx = text_context(text, **kw)
    out = run_rules(ctx, build_registry())
    return next(o for o in out.outcomes if o.rule.rule_id == rid), ctx


class DisclosureUpgradeTests(unittest.TestCase):
    def test_actual_debt_default_has_verified_quote_and_paper(self):
        o, ctx = evaluate_text('SV09', '本公司未能按期偿还借款本金人民币2亿元。')
        self.assertEqual(o.status, S.RISK)
        self.assertTrue(o.evidence_ids)
        self.assertTrue(all(ctx.evidence.get(eid).verified for eid in o.evidence_ids))
        self.assertEqual(o.workpaper['hits'][0]['page'], 1)
        self.assertEqual(o.to_result().metric_snapshot, o.workpaper)

    def test_debt_negations_hypotheticals_and_questions(self):
        for text in ['本公司不存在债务违约。', '如果本公司未能按期偿还借款，将触发交叉违约。',
                     '请说明本公司是否未能按期偿还借款。', '本公司可能未能按期偿还借款。',
                     '例如其他公司未能按期偿还借款。', '本公司债务未发生违约。',
                     '本公司持有的债券未能按期兑付。', '本公司主要客户未能按期偿还借款。']:
            with self.subTest(text=text):
                o, _ = evaluate_text('SV09', text)
                self.assertEqual(o.status, S.INSUFFICIENT)

    def test_linked_issuer_cannot_supply_company_evidence(self):
        ctx = text_context('本公司未能按期偿还借款。')
        ctx.docs[0].secucode = 'OTHER.HK'
        self.assertEqual(rule('SV09').evaluate(ctx).status, S.INSUFFICIENT)

    def test_real_provider_bare_code_matches_same_market(self):
        for market, source in [(Market.A, 'cninfo'), (Market.HK, 'hkexnews')]:
            ctx = text_context('本公司未能按期偿还借款。')
            ctx.market = market
            ctx.docs[0].secucode = ctx.security.code
            ctx.docs[0].source = source
            self.assertEqual(rule('SV09').evaluate(ctx).status, S.RISK)

    def test_bare_code_from_other_market_not_accepted(self):
        ctx = text_context('本公司未能按期偿还借款。')
        ctx.docs[0].secucode = ctx.security.code
        ctx.docs[0].source = 'hkexnews'
        self.assertEqual(rule('SV09').evaluate(ctx).status, S.INSUFFICIENT)

    def test_traditional_default(self):
        o, _ = evaluate_text('SV09', '本集團未能按期償還借款本金。')
        self.assertEqual(o.status, S.RISK)

    def test_occupation_and_guarantee_assertions(self):
        for text in ['本公司存在控股股东非经营性资金占用。', '本公司存在违规担保。']:
            with self.subTest(text=text):
                self.assertEqual(evaluate_text('GV07', text)[0].status, S.RISK)

    def test_occupation_denial_not_risk(self):
        for text in ['本公司不存在控股股东非经营性资金占用。', '本公司未发生违规担保。',
                     '本公司应当防范控股股东占用公司资金。']:
            self.assertEqual(evaluate_text('GV07', text)[0].status, S.INSUFFICIENT)

    def test_internal_control_defect_and_opinion(self):
        for text in ['本公司内部控制存在重大缺陷。', '本公司内部控制审计报告被出具否定意见。']:
            self.assertEqual(evaluate_text('GV08', text)[0].status, S.RISK)

    def test_internal_control_denial_not_risk(self):
        for text in ['本公司内部控制不存在重大缺陷。', '本公司内部控制未发现重大缺陷。',
                     '本公司内部控制审计未被出具否定意见。']:
            self.assertEqual(evaluate_text('GV08', text)[0].status, S.INSUFFICIENT)

    def test_receivable_quality_specific_disclosure(self):
        self.assertEqual(evaluate_text('FQ14', '本公司应收账款逾期，回收存在困难。')[0].status, S.WATCH)

    def test_receivable_hypothetical_not_risk(self):
        self.assertEqual(evaluate_text('FQ14', '如果本公司应收账款逾期，可能影响现金流。')[0].status, S.INSUFFICIENT)

    def test_impairment_warning(self):
        self.assertEqual(evaluate_text('OP06', '本公司商誉存在减值迹象。')[0].status, S.WATCH)

    def test_impairment_denial(self):
        self.assertEqual(evaluate_text('OP06', '本公司商誉不存在减值迹象。')[0].status, S.INSUFFICIENT)

    def test_customer_dependency(self):
        self.assertEqual(evaluate_text('OP07', '本公司主要客户终止合作。')[0].status, S.WATCH)

    def test_missing_pdf_and_title_only_not_confirmed(self):
        ctx = context(docs=[doc('关于债务违约的公告', '其他公告')])
        o = rule('SV09').evaluate(ctx)
        self.assertEqual(o.status, S.INSUFFICIENT)

    def test_real_financial_report_checkbox_templates_are_not_events(self):
        samples = [
            ('GV07', '二、报告期内控股股东及其他关联方非经营性占用资金情况\n□适用 √不适用\n三、违规担保情况\n□适用 √不适用'),
            ('GV08', '报告期内部控制存在重大缺陷情况的说明\n□适用 √不适用\n本公司实行集团一体化管理。'),
            ('FQ14', '本集团根据信贷风险分级评估，包括内部信用评级、应收账款及合同资产；无任何逾期未付款项。'),
            ('OP07', '测试公司2025年年度报告\n2.客户集中度过高或过低的风险\n下游行业市场具有集中度较高的特点。'),
            ('FQ14', '测试公司财务报表附注\n应收账款账面余额和坏账准备计提比例\n逾期组合为信用风险分级之一。'),
        ]
        for rid, text in samples:
            with self.subTest(rid=rid):
                self.assertEqual(evaluate_text(rid, text)[0].status, S.INSUFFICIENT)

    def test_report_denial_does_not_clear_another_event(self):
        o, _ = evaluate_text('SV09', '本公司不存在债务违约。本公司未能按期偿还借款。')
        self.assertEqual(o.status, S.RISK)

    def test_financial_industry_adverse_disclosure_enables_only_that_rule(self):
        cases = [('BK03', 'bank', '本公司不良贷款率大幅上升。'),
                 ('BK04', 'bank', '本公司资本充足率低于监管要求。'),
                 ('IN03', 'insurance', '本公司偿付能力不达标。'),
                 ('IN04', 'insurance', '本公司准备金计提不足。'),
                 ('BR03', 'broker', '本公司净资本低于监管要求。'),
                 ('BR04', 'broker', '本公司股票质押业务发生违约。')]
        for rid, pack, text in cases:
            with self.subTest(rid=rid):
                o, _ = evaluate_text(rid, text, pack=pack)
                self.assertIn(o.status, [S.RISK, S.WATCH])
                self.assertEqual(o.capability, 'enabled')

    def test_no_regulatory_fields_does_not_claim_supported(self):
        o, _ = evaluate_text('BK04', '本公司资本充足率满足监管要求。', pack='bank')
        self.assertEqual(o.capability, 'unsupported_source')


class ForecastTests(unittest.TestCase):
    def test_share_monthly_correction_not_financial_restatement(self):
        self.assertEqual(rule('FQ12').evaluate(context(docs=[doc('港股公告：证券变动月报表（更正）', '财务更正')])).status, S.NORMAL)
        from app.data.hkexnews import _classify_hk
        self.assertEqual(_classify_hk('證券變動月報表（更正）', ''), '月报表')

    def test_financial_report_correction_kept(self):
        self.assertEqual(rule('FQ12').evaluate(context(docs=[doc('关于2025年年度报告的更正公告', '财务更正')])).status, S.RISK)

    def test_unspecified_correction_not_claimed_financial_restatement(self):
        self.assertEqual(rule('FQ12').evaluate(context(docs=[doc('更正公告', '财务更正')])).status, S.INSUFFICIENT)

    def test_positive_title_no_penalty(self):
        for title in ['2026年度业绩预增公告', '2026年度业绩扭亏公告']:
            self.assertEqual(rule('OP01').evaluate(context(docs=[doc(title, '业绩预告')])).status, S.NORMAL)

    def test_negative_title_watch(self):
        self.assertEqual(rule('OP01').evaluate(context(docs=[doc('2026年度业绩预亏公告', '业绩预告')])).status, S.WATCH)

    def test_generic_title_needs_body(self):
        self.assertEqual(rule('OP01').evaluate(context(docs=[doc('2026年度业绩预告', '业绩预告')])).status, S.INSUFFICIENT)

    def test_positive_body(self):
        ctx = text_context('本公司预计报告期净利润增长50%。', kind='业绩预告', title='业绩预告')
        self.assertEqual(rule('OP01').evaluate(ctx).status, S.NORMAL)

    def test_negative_body(self):
        ctx = text_context('本公司预计报告期净利润下降50%。', kind='业绩预告', title='业绩预告')
        self.assertEqual(rule('OP01').evaluate(ctx).status, S.WATCH)


class AnnualAndDebtTests(unittest.TestCase):
    def annual(self, cash=(20, 20, 20), profit=(100, 100, 100), **kw):
        rows = []
        for i in range(3):
            p = f'{2023+i}-12-31'
            rows += [fact('ocf', p, cash[i], **kw), fact('net_profit', p, profit[i], **kw)]
        return context(rows)

    def test_three_year_cash_gap(self):
        ctx = self.annual()
        self.assertEqual(rule('FQ13').evaluate(ctx).status, S.WATCH)
        self.assertEqual(ctx.workpapers['FQ13']['cumulative_ocf'], 60)

    def test_exact_threshold_not_flagged(self):
        self.assertEqual(rule('FQ13').evaluate(self.annual(cash=(50, 50, 50))).status, S.NORMAL)

    def test_all_negative_cash(self):
        self.assertEqual(rule('FQ13').evaluate(self.annual(cash=(-1, -2, -3), profit=(-1, -2, -3))).status, S.WATCH)

    def test_negative_profit_ratio_na(self):
        self.assertEqual(rule('FQ13').evaluate(self.annual(profit=(-1, -2, -3))).status, S.NOT_APPLICABLE)

    def test_missing_year(self):
        ctx = self.annual()
        ctx = context([f for f in ctx.facts.facts if f.period_end != '2024-12-31'])
        self.assertEqual(rule('FQ13').evaluate(ctx).status, S.INSUFFICIENT)

    def test_nonconsecutive_year(self):
        ctx = self.annual()
        for f in ctx.facts.facts:
            if f.period_end == '2023-12-31': f.period_end = '2022-12-31'
        self.assertEqual(rule('FQ13').evaluate(context(ctx.facts.facts)).status, S.INSUFFICIENT)

    def test_currency_mismatch(self):
        ctx = self.annual()
        ctx.facts.facts[0].currency = 'HKD'
        self.assertEqual(rule('FQ13').evaluate(ctx).status, S.INSUFFICIENT)

    def test_unknown_currency(self):
        self.assertEqual(rule('FQ13').evaluate(self.annual(currency='UNKNOWN')).status, S.INSUFFICIENT)

    def test_interim_not_used(self):
        self.assertEqual(rule('FQ13').evaluate(self.annual(ptype=PeriodType.INTERIM)).status, S.INSUFFICIENT)

    def test_nan_not_used(self):
        self.assertEqual(rule('FQ13').evaluate(self.annual(cash=(float('nan'), 1, 1))).status, S.INSUFFICIENT)

    def debt(self, **values):
        return context([fact(k, '2025-12-31', v) for k, v in values.items()])

    def test_complete_debt_shortfall(self):
        ctx = self.debt(cash=100, restricted_cash=0, debt_due_within_one_year=120)
        self.assertEqual(rule('SV10').evaluate(ctx).status, S.WATCH)

    def test_complete_debt_threshold(self):
        ctx = self.debt(cash=100, restricted_cash=0, debt_due_within_one_year=100)
        self.assertEqual(rule('SV10').evaluate(ctx).status, S.NORMAL)

    def test_missing_component_never_zero(self):
        ctx = self.debt(cash=100, restricted_cash=0, short_term_borrowings=10, current_noncurrent_liabilities=10)
        self.assertEqual(rule('SV10').evaluate(ctx).status, S.INSUFFICIENT)

    def test_partial_debt_lower_bound_can_flag(self):
        ctx = self.debt(cash=100, restricted_cash=0, short_term_borrowings=10, current_noncurrent_liabilities=120)
        self.assertEqual(rule('SV10').evaluate(ctx).status, S.WATCH)
        self.assertEqual(ctx.workpapers['SV10']['known_debt_lower_bound'], 130)

    def test_complete_components_no_double_count_of_lease_bond(self):
        ctx = self.debt(cash=100, restricted_cash=0, short_term_borrowings=20, current_noncurrent_liabilities=30,
                        other_current_interest_debt=0, bonds_payable=1000, lease_liabilities=500)
        self.assertEqual(rule('SV10').evaluate(ctx).status, S.NORMAL)
        self.assertEqual(ctx.workpapers['SV10']['debt_due_within_one_year'], 50)

    def test_hk_borrowings_not_added_again(self):
        ctx = self.debt(cash=100, restricted_cash=0, short_term_borrowings=20, current_noncurrent_liabilities=30, other_current_interest_debt=0)
        ctx.market = Market.HK
        self.assertEqual(rule('SV10').evaluate(ctx).status, S.INSUFFICIENT)

    def test_zero_debt_na(self):
        self.assertEqual(rule('SV10').evaluate(self.debt(debt_due_within_one_year=0)).status, S.NOT_APPLICABLE)

    def test_no_usable_cash_not_normal(self):
        self.assertEqual(rule('SV10').evaluate(self.debt(cash=100, debt_due_within_one_year=50)).status, S.INSUFFICIENT)

    def test_realestate_contract_comparable_year(self):
        ctx = context([fact('contract_liabilities', '2025-12-31', 50), fact('contract_liabilities', '2024-12-31', 100)], pack='realestate')
        self.assertEqual(rule('RE04').evaluate(ctx).status, S.WATCH)

    def test_realestate_single_point_insufficient(self):
        ctx = context([fact('advance_receivables', '2025-12-31', 50)], pack='realestate')
        self.assertEqual(rule('RE04').evaluate(ctx).status, S.INSUFFICIENT)


class ScoringAndIntegrationTests(unittest.TestCase):
    def dimensions(self):
        return [{'results': [{'rule_id': rid, 'status': '发现风险', 'severity': '高'} for rid in ['FQ01', 'FQ02', 'SV01', 'RE02']]}]

    def test_legacy_scoring_unchanged(self):
        self.assertEqual(risk_signal_score(self.dimensions())['score'], 52)

    def test_new_scoring_max_per_family(self):
        d = self.dimensions(); original = copy.deepcopy(d)
        s = risk_signal_score(d, deduplicate=True)
        self.assertEqual(s['score'], 76)
        self.assertEqual(len(s['merged']), 2)
        self.assertEqual(d, original)

    def test_independent_risks_still_count(self):
        d = [{'results': [{'rule_id': r, 'status': '发现风险', 'severity': '高'} for r in ['SV09', 'GV07', 'GV08']]}]
        self.assertEqual(risk_signal_score(d, deduplicate=True)['score'], 64)

    def test_registry_keeps_all_original_and_eight_new(self):
        ids = [r.rule_id for r in build_registry().rules]
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(set(ids)), 60)
        for rid in ['FQ13','FQ14','SV09','SV10','GV07','GV08','OP06','OP07']:
            self.assertIn(rid, ids)

    def test_new_report_shows_badge_merges_and_workpaper(self):
        from tests.test_validation import _base_payload
        p = _base_payload(); p['scoring_version'] = '2'; p['report_version'] = '1.3'
        p['rule_version'] = '1.3'
        from app.core.models import RuleResult, Dimension
        p['dimensions'] = [{'dimension': Dimension.SOLVENCY.value, 'results': [
            RuleResult(r['rule_id'], r['rule_id'], Dimension.SOLVENCY, S.RISK, Severity.HIGH).to_dict()
            for r in self.dimensions()[0]['results']]}]
        p['rule_workpapers'] = {'FQ13': {'inputs': '<script>alert(1)</script>'}}
        html = render_inline(p)
        self.assertIn('grade-c', html)
        self.assertIn('合并计入', html)
        self.assertIn('规则审核底稿', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_complete_coverage_exposes_unsupported_denominator(self):
        out = run_rules(context(pack='bank'), build_registry())
        cov = out.coverage.to_dict()
        self.assertEqual(cov['capability_completeness'], round(cov['evaluated'] / (cov['applicable'] + cov['unsupported']), 4))

    def test_serious_document_preserved_within_budget(self):
        from app.engine.selection import select_documents
        docs = [doc('年度报告', '年报', f'a{i}') for i in range(30)] + [doc('关于债务逾期的公告', '其他公告', 'default')]
        selected, _ = select_documents(docs, max_total=5)
        self.assertEqual(len(selected), 5)
        self.assertIn('default', [d.doc_id for d in selected])

    def test_provenance_immutable_and_mapping_ref(self):
        from app.data.provenance import preserve_response
        from app.data.eastmoney import EastmoneyClient, _a_rows_to_facts, A_BALANCE_MAP
        with tempfile.TemporaryDirectory() as tmp, patch.object(settings, 'db_path', Path(tmp)/'app.db'):
            data = {'result': {'pages': 1, 'data': [{'REPORT_DATE': '2025-12-31', 'TOTAL_ASSETS': 100}]}}
            original = copy.deepcopy(data)
            client = Mock(); client.get_json.return_value = data
            rows = EastmoneyClient(client).query_all('RPT_DMSK_FN_BALANCE', '600519.SH', stage='finance')
            facts = _a_rows_to_facts(rows[0], Statement.BALANCE, A_BALANCE_MAP, 'RPT_DMSK_FN_BALANCE')
            ref = facts[0].raw_ref
            self.assertEqual(json.loads((Path(tmp)/ref).read_text()), data)
            self.assertEqual(data, original)
            preserve_response(data, url='https://example.org', params={'reportName': 'test'})
            self.assertEqual(len(list((Path(tmp)/'raw/eastmoney').glob('*.json'))), 1)
            self.assertTrue(list((Path(tmp)/'source_manifest').glob('*.json')))
            from app.core import db
            db.init_db()
            db.save_facts('lineage', facts)
            self.assertEqual(db.load_facts('lineage')[0]['raw_ref'], ref)


if __name__ == '__main__':
    unittest.main()
