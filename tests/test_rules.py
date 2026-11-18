"""Deterministic checks using supplied snapshots and controlled extraction proposals."""
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from meridian.attachments import extract_attachments
from meridian.context import ContextRetriever
from meridian.inbox import load_inbox
from meridian.internal import load_internal
from meridian.normalise import (discount_rate, exclusive_amount, money, money_text,
                                orbit_net_price, requested_date)
from meridian.pricing import PricingRules
from meridian.rules import apply_business_rules, validate_saved_preparations

PACK = Path(os.environ.get('MERIDIAN_TEST_INPUT_ROOT',
                          str(Path(__file__).resolve().parents[1] / 'data')))
REVIEW_DATE = date(2026, 10, 9)


def supported(text, evidence, basis='explicit_request', reason=None):
    return {'value': str(text), 'basis': basis, 'evidence_ids': [evidence], 'reason': reason}


def selection(email_id, name, kind='new_order_request', po=None):
    return {'email_id': email_id, 'customer': {'name_as_stated': name, 'account_id_as_stated': None},
            'action_type': kind, 'references': {'customer_po': po, 'orbit_order_number': None},
            'related_email_ids': [], 'reason': 'Controlled fixture, not real model output.', 'uncertainties': []}


class RuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal = load_internal(PACK)
        cls.inbox = load_inbox(PACK)
        cls.retriever = ContextRetriever(cls.internal, cls.inbox)

    def context(self, selected):
        email = self.retriever.emails[selected['email_id']]
        return self.retriever.prepare(selected, attachment_results=extract_attachments(PACK, email))

    def record(self, selected, context, proposal):
        plain = json.loads(json.dumps(context, default=asdict))
        return {'schema_version': '1.0', 'report_id': 'fixture-preparation',
                'generated_at': '2026-10-09T12:00:00+00:00', 'status': 'prepared',
                'trigger': {'email_id': selected['email_id'], 'received_at': context['current_request']['received']},
                'classification': selected, 'context': plain, 'report_alerts': plain['report_alerts'],
                'proposal': proposal, 'business_rules_applied': False}

    def new_record(self, selected, requested_lines, *, quotes=None, header=None, discounts=None, totals=None):
        context = self.context(selected)
        evidence = next((source.source_id for source in context['sources'].values()
                         if source.extraction_method == 'pdf_text'), context['current_request']['source_id'])
        rows = {row['sku']: row for row in context['catalogue_candidates']}
        lines = []
        for number, (sku, count) in enumerate(requested_lines, 1):
            row = rows[sku]
            quote = (quotes or {}).get(sku)
            lines.append({'line_id': f'L{number}', 'description': supported(row['description'], evidence),
                          'sku': supported(sku, row['source_id'], 'inferred_suggestion', 'Controlled catalogue match.'),
                          'quantity': supported(count, evidence),
                          'unit': supported(row['unit'], row['source_id'], 'recorded_internal'),
                          'quantity_basis': 'order_total',
                          'customer_quoted_price': {'amount': supported(quote[0], evidence), 'currency': 'GBP',
                                                    'vat_basis': quote[1], 'price_basis': 'per_unit'} if quote else None})
        proposal = {'email_id': selected['email_id'], 'action_type': selected['action_type'],
                    'summary': 'Controlled deterministic-rule fixture.',
                    'new_order': {'header': header or {}, 'lines': lines,
                                  'customer_quoted_totals': totals or [], 'customer_discounts': discounts or []},
                    'uncertainties': [], 'issues': []}
        return self.record(selected, context, proposal), evidence

    def reviewed(self, record, today=REVIEW_DATE):
        result = apply_business_rules(record, today=today)
        self.assertEqual(result['validation']['status'], 'validated', result['validation'])
        return result['validation']

    def test_half_up_vat_and_discount_formats(self):
        self.assertEqual(money('19.125'), Decimal('19.13'))
        self.assertEqual(exclusive_amount('22.74', 'inclusive'), Decimal('18.95'))
        self.assertEqual(discount_rate(0.05), discount_rate(5.0))
        self.assertEqual(discount_rate('1%'), Decimal('0.01'))
        self.assertEqual(orbit_net_price({'UPRC': 645, 'DISC': 5}), Decimal('6.13'))
        self.assertEqual(orbit_net_price({'UPRC': 1550, 'DISC': 0}), Decimal('15.50'))
        for value in ('NaN', 'Infinity', '1,2', True, 'TBC'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        with self.assertRaises(ValueError):
            discount_rate(1)

    def test_oakfield_contract_overrides_band_only_for_consumables(self):
        selected = selection('MSG-039', 'Oakfield Academy Trust', po='OAK-301')
        record, _ = self.new_record(selected, [('CON-BIN-BLK', 10), ('CON-SAN-5L', 6),
                                              ('CON-DT-5L', 6), ('CON-RA-5L', 4), ('SW-CB-SET', 2)])
        result = self.reviewed(record)
        lines = result['changes_to_apply'][0]['lines']
        self.assertEqual(lines[0]['price']['unit_price_ex_vat'], '19.13')
        self.assertEqual(lines[-1]['price']['unit_price_ex_vat'], '71.10')
        self.assertEqual(result['calculations']['net'], '530.06')
        self.assertEqual(result['calculations']['vat'], '106.01')
        self.assertEqual(result['calculations']['gross'], '636.07')
        self.assertTrue(all(line['price']['discount_already_included'] for line in lines))

    def test_contract_reference_requires_matching_quoted_line_prices(self):
        selected = selection('MSG-039', 'Oakfield Academy Trust', po='OAK-301')
        record, evidence = self.new_record(
            selected, [('CON-BIN-BLK', 10)], quotes={'CON-BIN-BLK': ('19.13', 'exclusive')})
        order = record['proposal']['new_order']
        order['customer_discounts'] = [
            {'description': supported('Prices per contract OAT/MER/2024', evidence)}]
        findings = {issue['code'] for issue in self.reviewed(record)['issues']}
        self.assertIn('recorded_contract_reference', findings)
        self.assertNotIn('customer_discount_unapproved', findings)
        order['lines'][0]['customer_quoted_price']['amount']['value'] = '18.00'
        findings = {issue['code'] for issue in self.reviewed(record)['issues']}
        self.assertIn('customer_discount_unapproved', findings)
        self.assertIn('price_conflict', findings)

    def test_urgency_and_relative_dates_use_distinct_representations(self):
        self.assertEqual(requested_date('ASAP'), {'raw': 'ASAP', 'kind': 'urgency'})
        result = requested_date('by Friday', received_date=date(2026, 10, 6))
        self.assertEqual((result['kind'], result['date'], result['relative_to']),
                         ('by', '2026-10-09', '2026-10-06'))
        with self.assertRaises(ValueError):
            requested_date('by Friday', received_date=date(2026, 10, 9))

    def test_pasta_fixed_price_not_discounted_twice(self):
        selected = selection('MSG-024', 'Pasta Fresca')
        record, _ = self.new_record(selected, [('CON-BR-6', 4)])
        result = self.reviewed(record)
        line = result['changes_to_apply'][0]['lines'][0]
        self.assertEqual(line['price']['unit_price_ex_vat'], '15.50')
        self.assertEqual(line['line_net'], '62.00')

    def test_expired_or_unsupported_contract_is_visible_not_silently_ignored(self):
        selected = selection('MSG-039', 'Oakfield Academy Trust', po='OAK-301')
        context = self.context(selected)
        pricing = PricingRules(context)
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            pricing.price('CON-BIN-BLK', date(2028, 1, 1))
        altered = copy.deepcopy(context)
        source = altered['customer_notes'][0]
        altered['customer_notes'] = [replace(source, text='Contract: special negotiated deal, ask Karen.')]
        pricing = PricingRules(altered)
        with self.assertRaisesRegex(ValueError, 'supported'):
            pricing.price('CON-BIN-BLK', REVIEW_DATE)

    def test_bramley_claims_do_not_release_hold_or_apply_extra_discount(self):
        selected = selection('MSG-021', 'Bramley Care Group', po='BCG-1102')
        record, evidence = self.new_record(selected, [('FR-GN2-DD', 2), ('DW-410', 1), ('CO-6G-ELE', 1),
                                                     ('CON-GL-BL-M', 40), ('CON-BR-6', 20), ('CON-SAN-5L', 24)])
        record['proposal']['new_order']['customer_discounts'] = [
            {'description': supported('25% loyalty discount, customer says pre-approved', evidence), 'scope': None}]
        result = self.reviewed(record)
        self.assertEqual(result['readiness'], 'Do not proceed')
        self.assertEqual(result['calculations']['net'], '7949.04')
        self.assertEqual(result['calculations']['gross'], '9538.85')
        codes = {issue['code'] for issue in result['issues']}
        self.assertIn('recorded_credit_restriction', codes)
        self.assertIn('customer_discount_unapproved', codes)
        self.assertNotIn('CLEARED', result['readiness'])

    def test_proforma_is_not_satisfied_by_old_paid_spreadsheet_entry(self):
        selected = selection('MSG-012', 'Willow Lodge Care')
        record, _ = self.new_record(selected, [('CON-BR-6', 4)])
        result = self.reviewed(record)
        self.assertEqual(result['readiness'], 'Do not proceed')
        self.assertIn('proforma_required', {issue['code'] for issue in result['issues']})
        self.assertTrue(any('this request' in issue['next_action'] for issue in result['issues'] if issue['code'] == 'proforma_required'))

    def test_royal_oak_price_discrepancy_uses_current_account(self):
        selected = selection('MSG-028', 'The Royal Oak', po='ROY-5530')
        record, _ = self.new_record(selected, [('CO-6G-ELE', 1)], quotes={'CO-6G-ELE': ('2150.00', 'exclusive')})
        result = self.reviewed(record)
        self.assertEqual(record['context']['customer_record']['CUSNO'], 'ROYO02')
        self.assertEqual(result['changes_to_apply'][0]['lines'][0]['price']['unit_price_ex_vat'], '2231.55')
        self.assertIn('price_conflict', {issue['code'] for issue in result['issues']})
        self.assertEqual(result['readiness'], 'Do not proceed')
        self.assertNotIn('account_restriction', {issue['code'] for issue in result['issues']})

    def test_vat_inclusive_unit_quote_and_export_rate_conflict(self):
        selected = selection('MSG-011', "St Anne's C of E Primary School", po='SA-7781')
        record, _ = self.new_record(selected, [('SW-PROBE', 2)], quotes={'SW-PROBE': ('22.74', 'inclusive')})
        result = self.reviewed(record)
        self.assertEqual(result['changes_to_apply'][0]['lines'][0]['customer_quoted_price_ex_vat'], '18.95')
        codes = {issue['code'] for issue in result['issues']}
        self.assertNotIn('price_conflict', codes)
        self.assertIn('vat_conflict', codes)
        self.assertIn('duplicate_entry_risk', codes)

    def amendment_record(self, *, additional=False, destination=False):
        selected = selection('MSG-042' if destination else 'MSG-020',
                             'Fenland Hotel Group' if destination else 'Harbour View',
                             kind='amendment_request', po='FHG-4422' if destination else 'HV-2291')
        context = self.context(selected)
        order = context['explicitly_matched_orders'][0]
        evidence = context['current_request']['source_id']
        if destination:
            changes = [{'change_id': 'C1', 'operation': 'replace', 'field': 'delivery_destination',
                        'target': {'orbit_order_number': order['header']['ORDNO']},
                        'proposed_value': supported('The Lamb', evidence), 'evidence_ids': [evidence]}]
        else:
            page = next(source for source in context['sources'].values() if source.extraction_method == 'pdf_text')
            line = next(line for line in order['lines'] if line['STKCD'] == 'CON-GL-BL-M')
            changes = [{'change_id': 'C1', 'operation': 'replace', 'field': 'line_quantity',
                        'target': {'orbit_order_number': order['header']['ORDNO'], 'orbit_line_number': line['LNNO'],
                                   'sku': supported(line['STKCD'], line['source_id'], 'recorded_internal')},
                        'proposed_value': supported('2' if additional else '12', page.source_id),
                        'unit': supported('boxes', page.source_id),
                        'quantity_basis': 'additional' if additional else 'replacement_total',
                        'evidence_ids': [page.source_id]},
                       {'change_id': 'C2', 'operation': 'replace', 'field': 'delivery_date',
                        'target': {'orbit_order_number': order['header']['ORDNO']},
                        'proposed_value': supported('21/10/2026', page.source_id), 'evidence_ids': [page.source_id]}]
        proposal = {'email_id': selected['email_id'], 'action_type': 'amendment_request', 'summary': 'Controlled amendment.',
                    'amendment': {'changes': changes}, 'uncertainties': [], 'issues': []}
        return self.record(selected, context, proposal)

    def test_harbour_view_total_and_additional_both_produce_twelve_not_twenty_two(self):
        for additional in (False, True):
            record = self.amendment_record(additional=additional)
            before = copy.deepcopy(record)
            result = self.reviewed(record)
            self.assertEqual(len(result['changes_to_apply']), 2)
            change = result['changes_to_apply'][0]
            self.assertEqual(change['old_value'], '10')
            self.assertEqual(change['proposed_value'], '12')
            self.assertEqual(change['unit'], 'box')
            self.assertEqual(change['proposed_line_net'], '73.56')
            self.assertEqual(result['changes_to_apply'][1]['old_value'], '2026-10-14')
            self.assertEqual(result['changes_to_apply'][1]['proposed_value']['date'], '2026-10-21')
            self.assertEqual(record, before)
            self.assertNotIn('new_order', result['changes_to_apply'][0])

    def test_destination_change_is_unverified_and_account_address_is_not_old_order_destination(self):
        record = self.amendment_record(destination=True)
        result = self.reviewed(record)
        change = result['changes_to_apply'][0]
        self.assertIsNone(change['old_value'])
        self.assertEqual(change['proposed_value']['value'], 'The Lamb')
        self.assertIn('Unverified', change['request_status'])
        self.assertEqual(result['readiness'], 'Do not proceed')
        self.assertIn('unverified_destination_change', {issue['code'] for issue in result['issues']})
        self.assertEqual(result['current_order_status'][0]['status_code_raw'], 'N')
        self.assertIn('Undocumented', result['current_order_status'][0]['status_meaning'])

    def test_resend_blocks_duplicate_entry(self):
        selected = selection('MSG-036', "St Anne's C of E Primary School", kind='resend', po='SA-7781')
        selected['related_email_ids'] = ['MSG-011']
        context = self.context(selected)
        proposal = {'email_id': 'MSG-036', 'action_type': 'resend', 'summary': 'Resubmission.',
                    'resend': {'related_email_ids': ['MSG-011'], 'explanation': 'Resubmitted PO.',
                               'evidence_ids': [context['current_request']['source_id']]},
                    'uncertainties': [], 'issues': []}
        result = self.reviewed(self.record(selected, context, proposal))
        self.assertEqual(result['changes_to_apply'], [])
        self.assertEqual(result['readiness'], 'Do not proceed')
        self.assertTrue(any('another order must not' in issue['description'] for issue in result['issues']))

    def test_missing_and_forged_evidence_fails_instead_of_fabricating_success(self):
        selected = selection('MSG-012', 'Willow Lodge Care')
        record, _ = self.new_record(selected, [('CON-BR-6', 4)])
        record['proposal']['new_order']['lines'][0]['quantity']['evidence_ids'] = ['MISSING']
        result = apply_business_rules(record)
        self.assertFalse(result['business_rules_applied'])
        self.assertEqual(result['validation']['status'], 'failed')
        self.assertEqual(result['validation']['readiness'], 'Do not proceed')
        self.assertEqual(result['validation']['changes_to_apply'], [])
        record, _ = self.new_record(selected, [('CON-BR-6', 4)])
        record['context']['catalogue_candidates'][0]['list_price_gbp'] = '0.01'
        self.assertEqual(apply_business_rules(record)['validation']['status'], 'failed')

    def test_incomplete_line_does_not_create_a_complete_order_total(self):
        selected = selection('MSG-024', 'Pasta Fresca')
        record, _ = self.new_record(selected, [('CON-BR-6', 4), ('CON-GL-BL-L', 2)])
        record['proposal']['new_order']['lines'][1]['quantity'] = None
        result = self.reviewed(record)
        self.assertFalse(result['calculations']['complete'])
        self.assertNotIn('gross', result['calculations'])
        self.assertEqual(result['readiness'], 'Do not proceed')

    def test_unknown_dates_and_windows_are_not_invented_delivery_dates(self):
        with self.assertRaises(ValueError):
            requested_date('before half term')
        with self.assertRaises(ValueError):
            requested_date('Friday')
        raw = 'w/c 19/10/2026 (ideally)'
        self.assertEqual(requested_date(raw)['end_date'], '2026-10-25')
        self.assertEqual(requested_date(raw)['raw'], raw)
        self.assertEqual(requested_date('Wednesday 21st (21/10/2026)')['date'], '2026-10-21')
        with self.assertRaises(ValueError):
            requested_date('Thursday 21st (21/10/2026)')
        self.assertEqual(requested_date('before 16/10/2026')['kind'], 'before')
        result = self.reviewed(self.amendment_record(), today=date(2026, 11, 18))
        self.assertIn('past_requested_date', {issue['code'] for issue in result['issues']})
        self.assertEqual(result['readiness'], 'Do not proceed')

    def test_review_files_are_retained_and_cli_needs_no_model_or_input_pack(self):
        from meridian.__main__ import main
        record = self.amendment_record(destination=True)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'preparation_fixture.json'
            source.write_text(json.dumps(record))
            before = source.read_bytes()
            first = validate_saved_preparations(source, directory)
            second = validate_saved_preparations(source, directory)
            self.assertNotEqual(first['results'][0]['path'], second['results'][0]['path'])
            self.assertEqual(source.read_bytes(), before)
            with patch('sys.argv', ['meridian', '--validate', str(source), '--output-dir', directory,
                                    '--input-root', 'does-not-exist']), patch('meridian.llm.create_client') as client, \
                    redirect_stdout(io.StringIO()) as stdout:
                main()
            client.assert_not_called()
            self.assertEqual(json.loads(stdout.getvalue())['validated'], 1)

    def test_manifest_missing_preparation_still_produces_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'preparation_run.json'
            manifest.write_text(json.dumps({'selected_emails': 1, 'results': [{'path': '/old/location/missing.json'}]}))
            result = validate_saved_preparations(manifest, directory)
            self.assertEqual(result['failed'], 1)
            saved = json.loads(Path(result['results'][0]['path']).read_text())
            self.assertEqual(saved['validation']['readiness'], 'Do not proceed')

    def test_input_snapshots_remain_unchanged(self):
        paths = [path for path in PACK.rglob('*') if path.is_file()]
        before = {path: hashlib.sha256(path.read_bytes()).digest() for path in paths}
        self.reviewed(self.amendment_record())
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).digest() for path in paths})

    def test_normal_unmatched_order_is_checks_required_not_automatically_ready(self):
        selected = selection('MSG-038', 'The Mill House')
        record, _ = self.new_record(selected, [('CON-BR-6', 1)])
        result = self.reviewed(record)
        self.assertEqual(result['readiness'], 'Checks required')
        self.assertEqual(result['entry_section_label'], 'Changes to apply')
        self.assertTrue(result['requires_human_review'])
        self.assertIn('existing_order_not_established', {item['code'] for item in result['issues']})

    def test_csv_band_price_is_used_as_recorded_including_rounding_difference(self):
        selected = selection('MSG-028', 'The Royal Oak', po='ROY-5530')
        record, _ = self.new_record(selected, [('SW-GN11-100', 8)],
                                     quotes={'SW-GN11-100': ('13.78', 'exclusive')})
        result = self.reviewed(record)
        self.assertEqual(result['changes_to_apply'][0]['lines'][0]['price']['unit_price_ex_vat'], '13.77')
        self.assertIn('price_conflict', {item['code'] for item in result['issues']})

    def test_unit_mismatch_is_not_converted_into_another_pack_quantity(self):
        selected = selection('MSG-024', 'Pasta Fresca')
        record, _ = self.new_record(selected, [('CON-BR-6', 4)])
        record['proposal']['new_order']['lines'][0]['unit']['value'] = 'box'
        result = self.reviewed(record)
        self.assertEqual(result['readiness'], 'Do not proceed')
        self.assertIn('line_pricing_unresolved', {item['code'] for item in result['issues']})
        self.assertNotIn('gross', result['calculations'])

    def test_old_account_prohibition_is_scoped_to_old_account(self):
        inbox = copy.deepcopy(self.inbox)
        email = next(email for email in inbox.emails if email['id'] == 'MSG-028')
        email['body'] += ' Account ROYO01.'
        inbox.sources[email['source_id']] = replace(inbox.sources[email['source_id']], text=email['body'])
        retriever = ContextRetriever(self.internal, inbox)
        selected = selection('MSG-028', None)
        selected['customer']['account_id_as_stated'] = 'ROYO01'
        with patch.object(self, 'retriever', retriever):
            record, _ = self.new_record(selected, [('CO-6G-ELE', 1)])
        self.assertEqual(record['context']['customer_record']['CUSNO'], 'ROYO01')
        result = self.reviewed(record)
        self.assertIn('account_restriction', {item['code'] for item in result['issues']})
        self.assertEqual(result['readiness'], 'Do not proceed')

    def test_unmapped_status_meaning_is_not_invented_from_h(self):
        selected = selection('MSG-015', 'Riverside Brasserie')
        record, _ = self.new_record(selected, [('CON-CF-450', 4)])
        self.assertEqual(record['context']['customer_record']['ACSTAT'], 'H')
        record['context']['open_items'] = []
        record['context']['report_alerts'] = []
        record['report_alerts'] = []
        result = self.reviewed(record)
        self.assertNotIn('recorded_credit_restriction', {item['code'] for item in result['issues']})
        self.assertIn('manual_credit_check', {item['code'] for item in result['issues']})

    def test_preparation_blocking_alerts_remain_blocking(self):
        selected = selection('MSG-038', 'The Mill House')
        record, _ = self.new_record(selected, [('CON-BR-6', 1)])
        record['report_alerts'].append({'code': 'fixture_block', 'title': 'Manual check',
                                        'message': 'Controlled unresolved source check.', 'blocks_entry': True})
        result = self.reviewed(record)
        self.assertIn('fixture_block', {item['code'] for item in result['issues']})
        self.assertEqual(result['readiness'], 'Do not proceed')

    def test_quantity_cannot_be_reduced_below_recorded_dispatch(self):
        record = self.amendment_record()
        change = record['proposal']['amendment']['changes'][0]
        change['proposed_value']['value'] = '1'
        line = next(line for line in record['context']['explicitly_matched_orders'][0]['lines']
                    if line['LNNO'] == change['target']['orbit_line_number'])
        line['QTYDS'] = 8
        source = record['context']['sources'][line['source_id']]
        original = json.loads(source['text']); original['QTYDS'] = 8; source['text'] = json.dumps(original)
        result = self.reviewed(record)
        self.assertIn('below_dispatched_quantity', {item['code'] for item in result['issues']})
        self.assertEqual(result['readiness'], 'Do not proceed')

    def test_vat_component_is_not_treated_as_grand_total_and_one_penny_conflict_is_visible(self):
        selected = selection('MSG-039', 'Oakfield Academy Trust', po='OAK-301')
        record, evidence = self.new_record(selected, [('CON-BIN-BLK', 10), ('CON-SAN-5L', 6),
                                                     ('CON-DT-5L', 6), ('CON-RA-5L', 4), ('SW-CB-SET', 2)])
        record['proposal']['new_order']['customer_quoted_totals'] = [
            {'label': 'VAT', 'amount': supported('106.01', evidence), 'currency': 'GBP',
             'price_basis': 'order_total', 'vat_basis': 'unknown'},
            {'label': 'Total', 'amount': supported('636.07', evidence), 'currency': 'GBP',
             'price_basis': 'order_total', 'vat_basis': 'inclusive'}]
        result = self.reviewed(record)
        self.assertNotIn('quoted_total_conflict', {item['code'] for item in result['issues']})
        record['proposal']['new_order']['customer_quoted_totals'][1]['amount']['value'] = '636.08'
        result = self.reviewed(record)
        self.assertIn('quoted_total_conflict', {item['code'] for item in result['issues']})

    def test_explicit_pack_size_conflict_is_not_silently_accepted(self):
        selected = selection('MSG-039', 'Oakfield Academy Trust', po='OAK-301')
        record, _ = self.new_record(selected, [('CON-BIN-BLK', 10)])
        record['proposal']['new_order']['lines'][0]['unit']['value'] = 'box of 100'
        result = self.reviewed(record)
        self.assertIn('line_pricing_unresolved', {item['code'] for item in result['issues']})
        self.assertEqual(result['readiness'], 'Do not proceed')


if __name__ == '__main__':
    unittest.main()
