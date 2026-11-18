import copy
import hashlib
import os
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from meridian.context import ContextRetriever, order_date
from meridian.inbox import load_inbox
from meridian.internal import load_internal
from meridian.sources import Source

PACK = Path(os.environ.get('MERIDIAN_TEST_INPUT_ROOT',
                          str(Path(__file__).resolve().parents[1] / 'data')))


def selection(email_id, name=None, po=None, order=None):
    return {'email_id': email_id, 'action_type': 'amendment_request',
            'customer': {'name_as_stated': name},
            'references': {'customer_po': po, 'orbit_order_number': order}}


class ContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.internal = load_internal(PACK)
        cls.inbox = load_inbox(PACK)
        cls.retriever = ContextRetriever(cls.internal, cls.inbox)

    def test_new_customer_po_miss_is_a_check_but_amendment_miss_blocks(self):
        action = selection('MSG-041', "King's Hall School", 'KH/2026/131')
        for action_type, blocked in [('new_order_request', False), ('amendment_request', True)]:
            context = self.retriever.prepare({**action, 'action_type': action_type})
            alert = next(a for a in context['report_alerts'] if a['code'] == 'order_reference_unresolved')
            self.assertEqual(alert['blocks_entry'], blocked)

    def test_account_notes_corroborate_documented_senders_and_sites(self):
        for email_id, name, account in [
            ('MSG-015', 'Luca Moretti', 'RIVR01'),
            ('MSG-038', 'Rachel Tunney', 'MILL01'),
            ('MSG-024', 'Pasta Fresca Hills Road', 'PAST01'),
        ]:
            with self.subTest(email_id=email_id):
                result = self.retriever.prepare(selection(email_id, name))['customer_match']
                self.assertEqual(result.account_id, account)
                self.assertTrue(any('customer_account_notes.md:section:' in key for key in result.evidence_ids))
        conflict = self.retriever.prepare(selection('MSG-038', 'Pasta Fresca'))
        self.assertEqual(conflict['customer_match'].status, 'unresolved')

    def test_mixed_dates_and_serials(self):
        for value in ('01/04/2025', '20250401', '2025-04-01', '45748', 45748):
            self.assertEqual(order_date(value), date(2025, 4, 1))
        for value in ('', None, 'not a date', '20260230'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                order_date(value)

    def test_revision_keeps_existing_order_separate_from_history(self):
        context = self.retriever.prepare(selection('MSG-020', 'Harbour View', 'HV-2291'))
        self.assertEqual(context['customer_match'].account_id, 'HARB01')
        existing = context['explicitly_matched_orders']
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0]['header']['CUSREF'], 'HV-2291')
        self.assertEqual(len(context['supporting_history']), 3)
        gloves = next(line for line in existing[0]['lines'] if line['STKCD'] == 'CON-GL-BL-M')
        self.assertEqual(gloves['QTY'], 10)  # Retrieval must not apply the revision.
        self.assertEqual(context['current_request']['id'], 'MSG-020')
        self.assertTrue(any(a['code'] == 'attachment_extraction_pending' for a in context['report_alerts']))
        self.assertTrue(all(o['header']['CUSNO'] == 'HARB01' for o in context['supporting_history']))

    def test_old_reference_is_retrieved_outside_last_three(self):
        old = min(self.internal.orders_by_customer['HARB01'], key=lambda o: order_date(o['ORDDT']))
        inbox = copy.deepcopy(self.inbox)
        email = inbox.emails[0]
        email['body'] = f"Amend order {old['ORDNO']} please"
        context = ContextRetriever(self.internal, inbox).prepare(
            selection(email['id'], 'Harbour View Hotel', order=str(old['ORDNO'])))
        self.assertEqual(context['explicitly_matched_orders'][0]['header']['ORDNO'], old['ORDNO'])
        self.assertNotIn(old['ORDNO'], [o['header']['ORDNO'] for o in context['supporting_history']])
        self.assertEqual(len(context['supporting_history']), 3)
        self.assertTrue(context['explicitly_matched_orders'][0]['lines'])

    def test_duplicate_account_resolved_only_by_explicit_notes(self):
        context = self.retriever.prepare(selection('MSG-028', 'The Royal Oak', 'ROY-5530'))
        self.assertEqual(context['customer_match'].account_id, 'ROYO02')
        self.assertTrue(any('Use ROYO02' in (s.text or '') for s in context['customer_notes']))
        self.assertTrue(all(o['header']['CUSNO'] == 'ROYO02' for o in context['supporting_history']))

    def test_unresolved_new_owner_is_prominent_and_gets_no_other_history(self):
        context = self.retriever.prepare(selection('MSG-017', 'Grafham Kitchen'))
        self.assertEqual(context['customer_match'].status, 'unresolved')
        self.assertIsNone(context['customer_record'])
        self.assertEqual(context['supporting_history'], [])
        self.assertEqual(context['open_items'], [])
        self.assertEqual(context['customer_notes'], [])
        alert = context['report_alerts'][0]
        self.assertEqual(alert['code'], 'customer_match_unresolved')
        self.assertTrue(alert['blocks_entry'])
        self.assertIn('report_header', alert['placement'])
        self.assertIn('do not proceed', alert['title'])

    def test_prefix_without_corroboration_stays_unresolved(self):
        context = self.retriever.prepare(selection('MSG-038', 'Mill'))
        self.assertEqual(context['customer_match'].status, 'unresolved')
        self.assertFalse(context['supporting_history'])

    def test_conflicting_name_and_order_do_not_assign_either_customer(self):
        context = self.retriever.prepare(selection('MSG-042', 'Harbour View Hotel', 'FHG-4422'))
        self.assertEqual(context['customer_match'].status, 'unresolved')
        self.assertEqual(set(context['customer_match'].candidate_account_ids), {'HARB01', 'FENS01'})
        self.assertFalse(context['supporting_history'])
        self.assertFalse(context['explicitly_matched_orders'])

    def test_reference_resolves_email_without_customer_name(self):
        context = self.retriever.prepare(selection('MSG-042', po='FHG-4422'))
        self.assertEqual(context['customer_match'].account_id, 'FENS01')
        self.assertEqual(context['explicitly_matched_orders'][0]['header']['CUSREF'], 'FHG-4422')
        self.assertIn('The Lamb', context['current_request']['body'])
        self.assertNotIn('MSG-044', str(context))
        self.assertFalse(any(source.source_id.startswith('email:MSG-044:') for source in context['sources'].values()))

    def test_fax_phone_identifies_account(self):
        context = self.retriever.prepare(selection('MSG-019'))
        self.assertEqual(context['customer_match'].account_id, 'GREE01')

    def test_st_annes_resend_retains_matched_order(self):
        context = self.retriever.prepare(selection('MSG-036', "St Anne's C of E Primary School", 'SA-7781'))
        self.assertEqual(context['customer_match'].account_id, 'STAN01')
        self.assertEqual(context['explicitly_matched_orders'][0]['header']['CUSREF'], 'SA-7781')
        self.assertNotIn('email:MSG-011:body', context['sources'])

    def test_unstated_reference_is_rejected(self):
        context = self.retriever.prepare(selection('MSG-038', 'The Mill House', po='HV-2291'))
        self.assertEqual(context['customer_match'].account_id, 'MILL01')
        self.assertFalse(context['explicitly_matched_orders'])
        self.assertTrue(any('not stated' in alert['message'] for alert in context['report_alerts']))

    def test_attachment_evidence_isolated_and_source_ids_resolve(self):
        email = self.retriever.emails['MSG-020']
        path = email['attachments'][0]['path']
        source = Source('test:revision:p1', path, 'Revised PO', 'page 1', None,
                        'Medium gloves 12 boxes total. Required by 21/10/2026.')
        context = self.retriever.prepare(selection('MSG-020', po='HV-2291'), [source])
        self.assertFalse(any(a['code'] == 'attachment_extraction_pending' for a in context['report_alerts']))
        self.assertEqual(context['sources'][source.source_id], source)
        for order in context['supporting_history'] + context['explicitly_matched_orders']:
            for row in [order['header'], *order['lines']]:
                self.assertIn(row['source_id'], context['sources'])
        unrelated = replace(source, relative_path='05_orders/PO_BCG-1102.pdf')
        with self.assertRaisesRegex(ValueError, 'outside trigger'):
            self.retriever.prepare(selection('MSG-020'), [unrelated])

    def test_history_sorted_by_dates_not_strings_and_raw_dates_preserved(self):
        context = self.retriever.prepare(selection('MSG-038', 'The Mill House'))
        expected = sorted(self.internal.orders_by_customer['MILL01'],
                          key=lambda row: (order_date(row['ORDDT']), row['ORDNO']), reverse=True)[:3]
        self.assertEqual([o['header'] for o in context['supporting_history']], expected)
        self.assertEqual([o['order_date_normalised'] for o in context['supporting_history']],
                         [order_date(o['ORDDT']).isoformat() for o in expected])

    def test_invalid_history_and_insufficient_support_visible(self):
        headers = [dict(self.internal.orders_by_customer['MILL01'][0]),
                   dict(self.internal.orders_by_customer['MILL01'][1])]
        headers[0]['ORDDT'] = 'unreadable'
        internal = replace(self.internal, orders_by_customer={'MILL01': headers})
        context = ContextRetriever(internal, self.inbox).prepare(selection('MSG-038', 'The Mill House'))
        self.assertEqual(len(context['supporting_history']), 1)
        self.assertEqual({a['code'] for a in context['report_alerts']},
                         {'invalid_history_dates', 'insufficient_history'})

    def test_duplicate_names_without_note_authority_remain_unresolved(self):
        customers = {key: dict(row) for key, row in self.internal.customers_by_id.items()}
        for key in ('HUNT01', 'CHER01'):
            customers[key]['CUSNM'] = 'Shared Trading Name'
        internal = replace(self.internal, customers_by_id=customers)
        context = ContextRetriever(internal, self.inbox).prepare(selection('MSG-038', 'Shared Trading Name'))
        self.assertEqual(context['customer_match'].status, 'unresolved')
        self.assertEqual(set(context['customer_match'].candidate_account_ids), {'HUNT01', 'CHER01', 'MILL01'})
        self.assertFalse(context['supporting_history'])
        self.assertTrue(context['report_alerts'][0]['blocks_entry'])

    def test_truncated_spreadsheet_customer_is_not_silently_assigned(self):
        context = self.retriever.prepare(selection('MSG-015', 'Riverside Brasserie'))
        uncertain = context['unresolved_open_items']
        self.assertTrue(any(row['Customer'] == 'Riverside Brass' for row in uncertain))
        self.assertFalse(any(row['Customer'] == 'Riverside Brass' for row in context['open_items']))
        self.assertTrue(any(a['code'] == 'spreadsheet_customer_unresolved' for a in context['report_alerts']))

    def test_unknown_trigger_fails_and_retrieval_does_not_write(self):
        with self.assertRaisesRegex(ValueError, 'Unknown trigger'):
            self.retriever.prepare(selection('UNKNOWN'))
        path = PACK / '06_orbit_export/orbit_nightly.sqlite'
        before = hashlib.sha256(path.read_bytes()).digest()
        self.retriever.prepare(selection('MSG-042', po='FHG-4422'))
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).digest())


if __name__ == '__main__':
    unittest.main()
