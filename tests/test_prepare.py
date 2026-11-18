import copy
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from meridian.attachments import extract_attachments
from meridian.context import ContextRetriever
from meridian.classification_artifacts import save_classification
from meridian.inbox import load_inbox
from meridian.internal import load_internal
from meridian.llm import LLMCallError
from meridian.prepare import prepare_classification, prepare_email
from meridian.preparation_schemas import PreparationOutput
from meridian.schemas import ActionType
from typing import get_args

PACK = Path(os.environ.get('MERIDIAN_TEST_INPUT_ROOT',
                          str(Path(__file__).resolve().parents[1] / 'data')))


def selection(email_id='MSG-012', kind='new_order_request', name='Willow Lodge Care', po=None):
    return {'email_id': email_id, 'customer': {'name_as_stated': name, 'account_id_as_stated': None},
            'action_type': kind, 'references': {'customer_po': po, 'orbit_order_number': None},
            'related_email_ids': [], 'reason': 'Synthetic test selection.', 'uncertainties': []}


def base_output(selected):
    return {'email_id': selected['email_id'], 'action_type': selected['action_type'],
            'summary': 'Synthetic preparation proposal; not a real model extraction.',
            'customer_po': None, 'new_order': None, 'amendment': None, 'resend': None,
            'unresolved_action': None, 'uncertainties': [], 'issues': []}


def value(text, evidence, basis='explicit_request', reason=None):
    return {'value': text, 'basis': basis, 'evidence_ids': [evidence], 'reason': reason}


def new_output(selected, context):
    result = base_output(selected)
    request = context['current_request']['source_id']
    catalogue = context['catalogue_candidates'][0]
    result['new_order'] = {
        'header': {'delivery_destination': None, 'requested_delivery_date': None, 'delivery_instructions': None},
        'lines': [{'line_id': 'L1', 'description': value('Requested item', request),
                   'sku': value(catalogue['sku'], catalogue['source_id'], 'inferred_suggestion',
                                'Synthetic catalogue match.'),
                   'quantity': value('4', request), 'unit': value('case', request),
                   'quantity_basis': 'order_total', 'customer_quoted_price': None}],
        'customer_quoted_totals': [], 'customer_discounts': []}
    return result


def response_body(output):
    return {'id': 'resp_synthetic', 'object': 'response', 'created_at': 0,
            'model': 'synthetic-test-model', 'status': 'completed', 'error': None,
            'output': [{'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text',
                                                          'text': json.dumps(output), 'annotations': []}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}}}


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inbox = load_inbox(PACK)
        cls.internal = load_internal(PACK)
        cls.retriever = ContextRetriever(cls.internal, cls.inbox)
        cls.selected = selection()
        cls.context = cls.retriever.prepare(cls.selected)

    def client(self, outputs):
        requests = []
        def handler(request):
            requests.append(request)
            output = outputs[len(requests) - 1]
            if isinstance(output, int):
                return httpx.Response(output, json={'error': {'message': 'Synthetic failure'}})
            return httpx.Response(200, json=response_body(output))
        client = OpenAI(api_key='synthetic-not-a-real-key', max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        return client, requests

    def test_new_order_proposals_evidence_metrics_and_no_readiness(self):
        output = new_output(self.selected, self.context)
        client, requests = self.client([output])
        result = prepare_email(self.selected, self.context, client=client, model='synthetic-test-model')
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['status'], 'prepared')
        self.assertFalse(result['business_rules_applied'])
        self.assertTrue(result['requires_human_review'])
        self.assertEqual(result['proposal']['new_order']['lines'][0]['quantity']['value'], '4')
        self.assertNotIn('amendment', result['proposal'])
        self.assertNotIn('readiness', result['proposal'])
        PreparationOutput.model_validate(result['proposal'])
        self.assertEqual(result['llm']['usage']['total_tokens'], 120)
        self.assertEqual(result['trigger']['received_at'], self.context['current_request']['received'])
        sent = json.loads(requests[0].content)
        self.assertTrue(sent['text']['format']['strict'])
        self.assertFalse(sent['store'])
        self.assertNotIn('tools', sent)
        payload = json.loads(sent['input'][1]['content'])
        self.assertEqual(payload['context']['email_id'], self.selected['email_id'])
        email_ids = [key for key in payload['context']['sources'] if key.startswith('email:')]
        self.assertEqual(email_ids, [self.context['current_request']['source_id']])

    def amendment(self):
        selected = selection('MSG-020', 'amendment_request', 'Harbour View', 'HV-2291')
        email = self.retriever.emails[selected['email_id']]
        context = self.retriever.prepare(selected, attachment_results=extract_attachments(PACK, email))
        order = context['explicitly_matched_orders'][0]
        line = next(line for line in order['lines'] if line['STKCD'] == 'CON-GL-BL-M')
        evidence = next(key for key, source in context['sources'].items()
                        if source.relative_path == email['attachments'][0]['path'] and source.extraction_method)
        output = base_output(selected)
        output['amendment'] = {'changes': [{
            'change_id': 'C1', 'operation': 'replace', 'field': 'line_quantity',
            'target': {'orbit_order_number': order['header']['ORDNO'], 'orbit_line_number': line['LNNO'],
                       'sku': value(line['STKCD'], line['source_id'], 'recorded_internal')},
            'proposed_value': value('12', evidence), 'unit': value('box', evidence),
            'quantity_basis': 'replacement_total', 'added_line': None, 'evidence_ids': [evidence]}]}
        return selected, context, output

    def test_amendment_retains_replacement_quantity_without_applying_it(self):
        selected, context, output = self.amendment()
        client, _ = self.client([output])
        result = prepare_email(selected, context, client=client, model='synthetic-test-model')
        change = result['proposal']['amendment']['changes'][0]
        self.assertEqual(change['quantity_basis'], 'replacement_total')
        self.assertEqual(change['proposed_value']['value'], '12')
        original = next(line for line in context['explicitly_matched_orders'][0]['lines']
                        if line['STKCD'] == 'CON-GL-BL-M')
        self.assertEqual(original['QTY'], 10)

    def test_invalid_references_and_unsupported_products_fail(self):
        original = new_output(self.selected, self.context)
        cases = []
        bad = copy.deepcopy(original); bad['email_id'] = 'MSG-044'; cases.append(bad)
        bad = copy.deepcopy(original); bad['new_order']['lines'][0]['quantity']['evidence_ids'] = ['EV-INVENTED']; cases.append(bad)
        bad = copy.deepcopy(original); bad['new_order']['lines'][0]['sku']['value'] = 'INVENTED-PRODUCT'; cases.append(bad)
        bad = copy.deepcopy(original); bad['new_order']['lines'][0]['sku']['reason'] = None; cases.append(bad)
        bad = copy.deepcopy(original)
        bad['new_order']['lines'][0]['quantity']['evidence_ids'] = [self.context['supporting_history'][0]['header']['source_id']]
        cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad):
                client, requests = self.client([bad])
                with self.assertRaises(LLMCallError):
                    prepare_email(self.selected, self.context, client=client, model='synthetic-test-model')
                self.assertEqual(len(requests), 1)

    def test_action_sections_and_quantity_basis_checked(self):
        selected, context, output = self.amendment()
        cases = []
        bad = copy.deepcopy(output); bad['amendment']['changes'][0]['quantity_basis'] = None; cases.append(bad)
        bad = copy.deepcopy(output); bad['resend'] = {'related_email_ids': [], 'explanation': 'Invalid mix',
                                                     'evidence_ids': [context['current_request']['source_id']]}; cases.append(bad)
        bad = copy.deepcopy(output); bad['readiness'] = 'approved'; cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad):
                client, _ = self.client([bad])
                with self.assertRaises(LLMCallError):
                    prepare_email(selected, context, client=client, model='synthetic-test-model')

    def test_history_order_cannot_become_amendment_target(self):
        selected, context, output = self.amendment()
        existing = context['explicitly_matched_orders'][0]['header']['ORDNO']
        history_order = next(order for order in context['supporting_history'] if order['header']['ORDNO'] != existing)
        output['amendment']['changes'][0]['target']['orbit_order_number'] = history_order['header']['ORDNO']
        client, _ = self.client([output])
        with self.assertRaisesRegex(LLMCallError, 'explicitly matched order'):
            prepare_email(selected, context, client=client, model='synthetic-test-model')

    def test_resend_contains_no_new_entry_lines(self):
        selected = selection('MSG-036', 'resend', "St Anne's C of E Primary School", 'SA-7781')
        selected['related_email_ids'] = ['MSG-011']
        context = self.retriever.prepare(selected)
        output = base_output(selected)
        output['resend'] = {'related_email_ids': ['MSG-011'], 'explanation': 'Resubmitted order; check existing entry.',
                            'evidence_ids': [context['current_request']['source_id']]}
        client, _ = self.client([output])
        result = prepare_email(selected, context, client=client, model='synthetic-test-model')
        self.assertIn('resend', result['proposal'])
        self.assertNotIn('new_order', result['proposal'])

    def test_unresolved_customer_alert_cannot_be_removed(self):
        selected = selection('MSG-017', name='Grafham Kitchen')
        context = self.retriever.prepare(selected)
        output = base_output(selected)
        output['new_order'] = {'header': {'delivery_destination': None, 'requested_delivery_date': None,
                                          'delivery_instructions': None}, 'lines': [],
                               'customer_quoted_totals': [], 'customer_discounts': []}
        output['uncertainties'] = [{'question': 'Which account should be used?', 'affected_fields': ['customer'],
                                    'next_action': 'Verify the new business account.',
                                    'evidence_ids': [context['current_request']['source_id']]}]
        client, _ = self.client([output])
        result = prepare_email(selected, context, client=client, model='synthetic-test-model')
        self.assertIsNone(result['context']['customer_record'])
        self.assertEqual(result['context']['supporting_history'], [])
        self.assertTrue(any(a['code'] == 'customer_match_unresolved' and a['blocks_entry'] for a in result['report_alerts']))

    def test_each_selected_email_gets_a_retained_result_or_failure(self):
        selections = [selection(), selection('MSG-024', name='Pasta Fresca'),
                      selection('MSG-038', name='The Mill House')]
        outputs = []
        for selected in selections:
            outputs.append(new_output(selected, self.retriever.prepare(selected)))
        outputs[1] = 500
        client, requests = self.client(outputs)
        with tempfile.TemporaryDirectory() as directory:
            result = prepare_classification({'actions': selections}, inbox=self.inbox, internal=self.internal,
                                            input_root=PACK, client=client, model='synthetic-test-model',
                                            output_dir=directory)
            self.assertEqual(len(requests), 3)
            self.assertEqual(result['call_count'], 3)
            self.assertEqual(result['prepared'], 2)
            self.assertEqual(result['failed'], 1)
            self.assertEqual(result['usage_available_for_calls'], 2)
            self.assertEqual(result['usage_totals_when_available']['total_tokens'], 240)
            for selected, entry, request in zip(selections, result['results'], requests):
                saved = json.loads(Path(entry['path']).read_text())
                self.assertEqual(saved['trigger']['email_id'], selected['email_id'])
                if saved['status'] == 'failed':
                    self.assertNotIn('proposal', saved)
                payload = json.loads(json.loads(request.content)['input'][1]['content'])
                self.assertEqual(payload['context']['email_id'], selected['email_id'])
                self.assertNotIn('email:MSG-044:body', payload['context']['sources'])
            first_paths = {path: path.read_bytes() for path in Path(directory).glob('*.json')}
            second_client, _ = self.client([outputs[0], outputs[2]])
            second = prepare_classification({'actions': [selections[0], selections[2]]}, inbox=self.inbox,
                                            internal=self.internal, input_root=PACK, client=second_client,
                                            model='synthetic-test-model', output_dir=directory)
            self.assertEqual(second['prepared'], 2)
            self.assertTrue(all(path.read_bytes() == content for path, content in first_paths.items()))
            self.assertNotEqual(result['manifest_path'], second['manifest_path'])

    def test_duplicate_triggers_fail_before_model_calls(self):
        client, requests = self.client([])
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'repeats'):
            prepare_classification({'actions': [self.selected, self.selected]}, inbox=self.inbox,
                                   internal=self.internal, input_root=PACK, client=client,
                                   model='synthetic-test-model', output_dir=directory)
        self.assertFalse(requests)

    def test_schema_generated_from_types_and_no_approval_fields(self):
        schema = PreparationOutput.model_json_schema()
        self.assertFalse(schema['additionalProperties'])
        self.assertNotIn('readiness', schema['properties'])
        self.assertNotIn('customer_account_id', schema['properties'])
        self.assertIn('new_order', schema['properties'])

    def test_context_failure_has_zero_calls_and_preserves_trigger_timestamp(self):
        client, requests = self.client([])
        with tempfile.TemporaryDirectory() as directory, patch(
                'meridian.prepare.extract_attachments', side_effect=ValueError('Synthetic extraction failure')):
            summary = prepare_classification({'actions': [self.selected]}, inbox=self.inbox,
                                             internal=self.internal, input_root=PACK, client=client,
                                             model='synthetic-test-model', output_dir=directory)
            record = json.loads(Path(summary['results'][0]['path']).read_text())
            self.assertEqual(summary['failed'], 1)
            self.assertEqual(summary['call_count'], 0)
            self.assertEqual(record['trigger']['received_at'], self.context['current_request']['received'])
            self.assertNotIn('proposal', record)
        self.assertFalse(requests)

    def test_customer_po_and_line_identifiers_are_checked(self):
        selected, context, original = self.amendment()
        bad_po = copy.deepcopy(original)
        bad_po['customer_po'] = value('INVENTED-PO', context['current_request']['source_id'])
        bad_line = copy.deepcopy(original)
        bad_line['amendment']['changes'][0]['target']['orbit_line_number'] = -999
        for bad in (bad_po, bad_line):
            with self.subTest(bad=bad):
                client, _ = self.client([bad])
                with self.assertRaises(LLMCallError):
                    prepare_email(selected, context, client=client, model='synthetic-test-model')

    def test_recorded_catalogue_replacement_is_available_as_context(self):
        selected = selection('MSG-037', 'new_order_request', 'The Salt Kitchen', 'TSK-88')
        email = self.retriever.emails[selected['email_id']]
        context = self.retriever.prepare(selected, attachment_results=extract_attachments(PACK, email))
        obsolete = self.internal.prices_by_sku['DW-400']
        replacement = obsolete['replaced_by']
        self.assertTrue(replacement)
        self.assertIn(replacement, {row['sku'] for row in context['catalogue_candidates']})
        self.assertIn(self.internal.prices_by_sku[replacement]['source_id'], context['sources'])

    def test_cli_preparation_uses_saved_classification_without_reclassifying(self):
        import io
        from contextlib import redirect_stdout
        from meridian.__main__ import main
        selected = self.selected
        output = new_output(selected, self.context)
        client, requests = self.client([output])
        counts = {kind: int(kind == selected['action_type']) for kind in get_args(ActionType)}
        classification = {'schema_version': '1.0', 'metadata': {
            'total_emails_read': len(self.inbox.emails), 'total_emails_requiring_action': 1,
            'total_emails_excluded': len(self.inbox.emails) - 1, 'breakdown_by_action': counts},
            'actions': [selected]}
        with tempfile.TemporaryDirectory() as directory:
            path = save_classification(classification, directory)
            with patch('sys.argv', ['meridian', '--input-root', str(PACK), '--prepare', str(path),
                                    '--output-dir', directory]), patch.dict(os.environ, {
                                        'OPENAI_API_KEY': 'synthetic', 'OPENAI_MODEL': 'synthetic-test-model'}), \
                    patch('meridian.llm.create_client', return_value=client), \
                    redirect_stdout(io.StringIO()) as stdout:
                main()
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary['prepared'], 1)
            self.assertEqual(len(requests), 1)
            self.assertTrue(Path(summary['results'][0]['path']).exists())

    def test_rejected_parsed_proposal_is_retained_without_accepting_it(self):
        output = new_output(self.selected, self.context)
        line = output['new_order']['lines'][0]
        line['sku']['reason'] = None
        client, requests = self.client([output])
        with tempfile.TemporaryDirectory() as directory:
            summary = prepare_classification({'actions': [self.selected]}, inbox=self.inbox,
                internal=self.internal, input_root=PACK, client=client,
                model='synthetic-test-model', output_dir=directory)
            record = json.loads(Path(summary['results'][0]['path']).read_text())
        self.assertEqual(len(requests), 1)
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('proposal', record)
        self.assertEqual(record['rejected_proposal']['new_order']['lines'][0]['sku']['value'], line['sku']['value'])
        self.assertIn('explanation', record['error'])


if __name__ == '__main__':
    unittest.main()
