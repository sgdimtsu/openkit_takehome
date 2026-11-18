"""Presentation checks using controlled proposals, not real model classifications."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from meridian.artifacts import save_json_artifact
from meridian.pipeline import run_pipeline
from meridian.render import load_reviews, render_html, render_saved_reviews
from meridian.rules import apply_business_rules, validate_saved_preparations
import test_rules as rule_fixtures
selection, supported, PACK = rule_fixtures.selection, rule_fixtures.supported, rule_fixtures.PACK
from test_classify import response_body as classification_response
from test_prepare import response_body as preparation_response


class ParsedHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
    def handle_data(self, data):
        self.text.append(data)


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rule_fixtures.RuleTests.setUpClass()
        cls.fixtures = rule_fixtures.RuleTests('runTest')
        cls.amendment_record = cls.fixtures.amendment_record()
        cls.amendment = apply_business_rules(cls.amendment_record)
        cls.destination = apply_business_rules(cls.fixtures.amendment_record(destination=True))
        cls.new_record, _ = cls.fixtures.new_record(selection('MSG-038', 'The Mill House'), [('CON-BR-6', 1)])
        cls.new = apply_business_rules(cls.new_record)

    def html(self, reports):
        return render_html(reports, input_root=PACK, output_dir=PACK.parent / 'output')

    def test_grouping_separate_triggers_and_fixed_sections(self):
        second = copy.deepcopy(self.amendment)
        second['report_id'] = 'second-harbour-review'
        second['preparation']['trigger']['email_id'] = 'SECOND-TRIGGER'
        text = self.html([self.amendment, second, self.destination])
        parsed = ParsedHTML(text)
        articles = [attrs for tag, attrs in parsed.tags if tag == 'article']
        groups = [attrs for tag, attrs in parsed.tags if tag == 'section' and attrs.get('class') == 'customer']
        self.assertEqual(len(articles), 3)
        self.assertEqual(len(groups), 2)
        for title in ('Current order status', 'Outstanding issues', 'Evidence'):
            self.assertIn(title, text)
        self.assertIn('SECOND-TRIGGER', text)
        self.assertIn('HARB01', text)
        self.assertIn('FENS01', text)
        self.assertIn('2026-10-21', text)
        self.assertIn('12 boxes total', ''.join(parsed.text))
        self.assertNotIn('22 boxes total', text)

    def test_source_labels_locations_timestamps_and_safe_pdf_links(self):
        text = self.html([self.amendment])
        parsed = ParsedHTML(text)
        self.assertIn('PO_HV-2291_rev.pdf', text)
        self.assertIn('Page 1', text)
        self.assertIn('Matched order 48177', text)
        self.assertIn('Supporting history', text)
        self.assertIn('timezone unknown', text)
        self.assertIn('Unknown freshness', text)
        self.assertTrue(any(tag == 'a' and attrs.get('href', '').endswith('PO_HV-2291_rev.pdf#page=1')
                            for tag, attrs in parsed.tags))
        self.assertTrue(all('open' not in attrs for tag, attrs in parsed.tags if tag == 'details'))
        targets = {attrs['id'] for tag, attrs in parsed.tags if 'id' in attrs}
        links = [attrs['href'][1:] for tag, attrs in parsed.tags if tag == 'a' and 'data-evidence' in attrs]
        self.assertTrue(links)
        self.assertTrue(all(target in targets for target in links))

    def test_source_content_is_escaped_in_text_attributes_and_links(self):
        reviewed = copy.deepcopy(self.new)
        malicious = '</script><script>window.__source_executed=1</script><img src="https://bad.invalid/x" onerror="alert(1)">'
        reviewed['validation']['issues'][0]['description'] = malicious
        source = next(iter(reviewed['preparation']['context']['sources'].values()))
        source['text'] = malicious
        source['display_name'] = malicious
        source['relative_path'] = 'javascript:alert(1)'
        text = self.html([reviewed])
        parsed = ParsedHTML(text)
        self.assertEqual(sum(tag == 'script' for tag, _ in parsed.tags), 1)  # Static evidence navigation only.
        self.assertFalse(any(tag in {'img', 'iframe', 'base'} for tag, _ in parsed.tags))
        self.assertFalse(any(key.startswith('on') for _, attrs in parsed.tags for key in attrs))
        self.assertFalse(any(attrs.get('href', '').startswith(('javascript:', 'https:')) for tag, attrs in parsed.tags if tag == 'a'))
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('window.__source_executed', ''.join(parsed.text))

    def test_failed_validation_never_displays_entry_values(self):
        record = copy.deepcopy(self.amendment_record)
        record['proposal']['amendment']['changes'][0]['evidence_ids'] = ['INVENTED']
        reviewed = apply_business_rules(record)
        self.assertEqual(reviewed['validation']['status'], 'failed')
        text = self.html([reviewed])
        self.assertIn('Validation failed', text)
        self.assertIn('Do not proceed', text)
        self.assertIn('No validated entry values', text)
        self.assertNotIn('12 boxes total', text)

    def test_destination_old_value_stays_unknown_and_request_unverified(self):
        text = self.html([self.destination])
        self.assertIn('The Lamb', text)
        self.assertIn('Not available in the order export', text)
        self.assertIn('Unverified customer request', text)
        self.assertIn('Checks required', text)

    def test_unresolved_customer_prominent_and_unsupported_input_is_failure(self):
        selected = selection('MSG-017', 'Grafham Kitchen')
        context = self.fixtures.context(selected)
        record = self.fixtures.record(selected, context, {'email_id': selected['email_id'], 'action_type': 'new_order_request',
            'summary': 'Controlled fixture.', 'new_order': {'header': {}, 'lines': [], 'customer_quoted_totals': [],
                                                         'customer_discounts': []}, 'uncertainties': [], 'issues': []})
        reviewed = apply_business_rules(record)
        text = self.html([reviewed])
        self.assertIn('Customer/account not established', text)
        self.assertIn('No account assigned', text)
        self.assertEqual(sum(tag == 'article' for tag, _ in ParsedHTML(text).tags), 1)
        self.assertIn('Customer/account unresolved', text)
        self.assertIn('Do not proceed', text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unvalidated.json'
            path.write_text(json.dumps(record))
            reviews = load_reviews(path)
            self.assertEqual(reviews[0]['validation']['status'], 'failed')

    def test_retention_previous_run_links_and_missing_input_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = save_json_artifact(self.new, directory, prefix='validated_preparation')
            first = render_saved_reviews(path, output_dir=directory, input_root=PACK)
            first_bytes = Path(first['path']).read_bytes()
            second = render_saved_reviews(path, output_dir=directory, input_root=PACK)
            self.assertNotEqual(first['path'], second['path'])
            self.assertEqual(Path(first['path']).read_bytes(), first_bytes)
            self.assertIn(Path(first['path']).name, Path(second['path']).read_text())
            manifest = Path(directory) / 'validation_run.json'
            manifest.write_text(json.dumps({'validated': 1, 'failed': 0, 'results': [
                {'path': '/old/location/missing.json', 'email_id': 'MISSING-TRIGGER'}]}))
            result = render_saved_reviews(manifest, output_dir=directory, input_root=PACK)
            text = Path(result['path']).read_text()
            self.assertEqual(result['validation_failures'], 1)
            self.assertIn('MISSING-TRIGGER', text)
            self.assertIn('Cannot use reviewed input', text)

    def test_manifest_renderer_and_cli_do_not_call_openai(self):
        from meridian.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            prep = save_json_artifact(self.amendment_record, directory, prefix='preparation')
            validation = validate_saved_preparations(prep, directory)
            with patch('sys.argv', ['meridian', '--render', validation['manifest_path'], '--output-dir', directory,
                                    '--input-root', str(PACK)]), patch('meridian.llm.create_client') as client, \
                    redirect_stdout(io.StringIO()) as stdout:
                main()
            client.assert_not_called()
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['reports'], 1)
            self.assertTrue(Path(result['path']).exists())

    def test_resend_has_no_new_order_table_and_exact_warning(self):
        selected = selection('MSG-036', "St Anne's C of E Primary School", kind='resend', po='SA-7781')
        selected['related_email_ids'] = ['MSG-011']
        context = self.fixtures.context(selected)
        record = self.fixtures.record(selected, context, {'email_id': selected['email_id'], 'action_type': 'resend',
            'summary': 'Controlled resend fixture.', 'resend': {'related_email_ids': ['MSG-011'], 'explanation': 'Resubmitted order.',
            'evidence_ids': [context['current_request']['source_id']]}, 'uncertainties': [], 'issues': []})
        text = self.html([apply_business_rules(record)])
        self.assertIn('Do not create another order.', text)
        self.assertNotIn('class="lines"', text)

    def test_full_pipeline_one_classification_plus_one_call_per_email(self):
        records = [self.amendment_record, self.new_record]
        selections = [record['classification'] for record in records]
        requests = []
        outputs = [classification_response(selections), preparation_response(records[0]['proposal']),
                   preparation_response(records[1]['proposal'])]
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=outputs[len(requests) - 1])
        with OpenAI(api_key='synthetic-not-a-real-key', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client, tempfile.TemporaryDirectory() as directory:
            summary = run_pipeline(input_root=PACK, output_dir=directory, client=client, model='synthetic-test-model')
            self.assertEqual(len(requests), 3)
            self.assertEqual(summary['calls'], {'classification': 1, 'preparation': 2, 'total': 3})
            self.assertEqual(summary['selected_emails'], 2)
            self.assertEqual(summary['validation_failures'], 0)
            text = Path(summary['html_path']).read_text()
            self.assertEqual(sum(tag == 'article' for tag, _ in ParsedHTML(text).tags), 2)
            self.assertTrue(Path(summary['classification_path']).exists())
            self.assertTrue(Path(summary['preparation_manifest_path']).exists())
            self.assertTrue(Path(summary['validation_manifest_path']).exists())
            self.assertTrue(Path(summary['manifest_path']).exists())
            self.assertEqual(summary['usage_totals_when_available']['total_tokens'], 360)

    def test_full_pipeline_keeps_a_preparation_failure_visible(self):
        records = [self.amendment_record, self.new_record]
        requests = []
        outputs = [classification_response([record['classification'] for record in records]),
                   preparation_response(records[0]['proposal']), None]
        def handler(request):
            requests.append(request)
            output = outputs[len(requests) - 1]
            return httpx.Response(200, json=output) if output else httpx.Response(
                500, json={'error': {'message': 'Controlled API failure'}})
        with OpenAI(api_key='synthetic-not-a-real-key', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client, tempfile.TemporaryDirectory() as directory:
            summary = run_pipeline(input_root=PACK, output_dir=directory, client=client, model='synthetic-test-model')
            self.assertEqual(len(requests), 3)
            self.assertEqual(summary['preparation_failures'], 1)
            self.assertEqual(summary['validation_failures'], 1)
            text = Path(summary['html_path']).read_text()
            self.assertEqual(sum(tag == 'article' for tag, _ in ParsedHTML(text).tags), 2)
            self.assertIn('Preparation failed', text)
            self.assertIn('1 processing failures', text)
            self.assertIn('MSG-038', text)
            self.assertIn('No validated entry values', text)

    def test_blanket_data_notices_are_not_rendered_as_tasks_even_for_saved_reviews(self):
        reviewed = copy.deepcopy(self.new)
        for code, message in [('manual_credit_check', 'Blanket credit recheck'),
                              ('snapshot_limitations', 'Blanket freshness warning'),
                              ('vat_conflict', 'Blanket VAT warning')]:
            reviewed['validation']['issues'].append({'code': code, 'description': message,
                'next_action': 'Generic task', 'owner': 'Order processor', 'blocks_entry': False,
                'evidence_ids': [], 'origin': 'deterministic_rule'})
        text = self.html([reviewed])
        for message in ['Blanket credit recheck', 'Blanket freshness warning', 'Blanket VAT warning']:
            self.assertNotIn(message, text)
        self.assertNotIn('Check current credit status in Orbit;', text)



if __name__ == '__main__':
    unittest.main()
