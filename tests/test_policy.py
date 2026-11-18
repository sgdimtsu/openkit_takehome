"""Shared configuration behaviour, including a fully mocked pipeline."""
import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from meridian.normalise import exclusive_amount, money, money_text, orbit_net_price
from meridian.pipeline import run_pipeline
from meridian.policy import SharedPolicy, load_policy, percent_text
from meridian.render import render_html
from meridian.rules import apply_business_rules
import test_rules as fixtures
from test_classify import response_body as classification_response
from test_prepare import response_body as preparation_response


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RuleTests.setUpClass()
        cls.fixture = fixtures.RuleTests('runTest')
        cls.record, _ = cls.fixture.new_record(
            fixtures.selection('MSG-038', 'The Mill House'), [('CON-BR-6', 1)])

    def policy(self, rate='0.05', precision=2):
        raw = load_policy().model_dump()
        raw['pricing'].update(vat_rate=rate, decimal_places=precision)
        return SharedPolicy.model_validate(raw)

    def test_legacy_policy_fingerprint_and_confirmed_status_meanings(self):
        import hashlib
        policy = load_policy()
        self.assertEqual(policy.review.account_status_codes, {'A': 'active', 'H': 'on_stop', 'C': 'closed'})
        raw = policy.model_dump()
        raw.pop('source_authority')
        raw['review'].pop('account_status_codes')
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(SharedPolicy.model_validate(raw).fingerprint, fingerprint)

    def test_invalid_and_unsupported_configuration_is_rejected(self):
        for field, value in [('vat_rate', 'NaN'), ('vat_rate', '-0.1'), ('vat_rate', '1.1'),
                             ('vat_rate', 0.2), ('rounding', 'ROUND_DOWN'), ('currency', 'USD'),
                             ('decimal_places', -1), ('orbit_price_unit', 'pounds'),
                             ('contract_overrides_band', False)]:
            with self.subTest(field=field, value=value):
                raw = load_policy().model_dump()
                raw['pricing'][field] = value
                with self.assertRaises(ValueError):
                    SharedPolicy.model_validate(raw)
        raw = load_policy().model_dump()
        raw['review']['customer_claims_authorise_exceptions'] = True
        with self.assertRaises(ValueError):
            SharedPolicy.model_validate(raw)
        raw['unknown_setting'] = True
        with self.assertRaises(ValueError):
            SharedPolicy.model_validate(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                load_policy(path)
            with self.assertRaises(OSError):
                load_policy(Path(directory) / 'missing.json')

    def test_all_monetary_helpers_use_selected_policy(self):
        policy = self.policy()
        self.assertEqual(exclusive_amount('105', 'inclusive', policy=policy), Decimal('100.00'))
        precision = self.policy(precision=3)
        self.assertEqual(money('1.2345', policy=precision), Decimal('1.235'))
        self.assertEqual(money_text('1.2', policy=precision), '1.200')
        self.assertEqual(orbit_net_price({'UPRC': 645, 'DISC': 5}, policy=precision), Decimal('6.128'))
        self.assertEqual([percent_text(x) for x in ('0', '0.05', '0.20', '1')], ['0', '5', '20', '100'])

    def test_vat_calculations_conflict_and_html_follow_configured_rate(self):
        reviewed = apply_business_rules(self.record, policy=self.policy())
        self.assertEqual(reviewed['validation']['status'], 'validated')
        totals = reviewed['validation']['calculations']
        self.assertEqual(Decimal(totals['vat']), money(Decimal(totals['net']) * Decimal('0.05')))
        self.assertEqual(totals['vat_rate'], '0.05')
        resolution = next(i for i in reviewed['validation']['resolutions'] if i['field'] == 'vat_rate')
        self.assertEqual(resolution['value'], '0.05')
        self.assertEqual(resolution['decision'], self.policy().pricing.vat_authority.decision)
        self.assertNotIn('vat_conflict', {i['code'] for i in reviewed['validation']['issues']})
        text = render_html([reviewed], input_root=fixtures.PACK, output_dir=fixtures.PACK.parent)
        self.assertIn('VAT (5%)', text)
        self.assertNotIn('VAT (20%)', text)
        self.assertEqual(reviewed['policy_origin'], 'validation_time_policy_for_legacy_preparation')

    def test_saved_policy_is_used_and_conflicting_override_or_tampering_fails(self):
        record = copy.deepcopy(self.record)
        policy = self.policy()
        record.update(shared_policy=policy.model_dump(), policy_sha256=policy.fingerprint)
        with patch('meridian.policy.load_policy', side_effect=AssertionError('Must use retained policy')):
            reviewed = apply_business_rules(record)
        self.assertEqual(reviewed['validation']['calculations']['vat_rate'], '0.05')
        self.assertEqual(reviewed['policy_origin'], 'preparation_snapshot')
        mismatch = apply_business_rules(record, policy=load_policy())
        self.assertEqual(mismatch['validation']['status'], 'failed')
        self.assertIn('differs', mismatch['validation']['error'])
        record['shared_policy']['pricing']['vat_rate'] = '0.10'
        self.assertEqual(apply_business_rules(record)['validation']['status'], 'failed')
        self.assertEqual(self.record.get('shared_policy'), None)

    def test_mocked_pipeline_uses_identical_policy_in_request_review_and_html(self):
        policy = self.policy()
        requests = []
        outputs = [classification_response([self.record['classification']]),
                   preparation_response(self.record['proposal'])]
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=outputs[len(requests) - 1])
        with OpenAI(api_key='synthetic-not-a-real-key', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client, tempfile.TemporaryDirectory() as directory:
            summary = run_pipeline(input_root=fixtures.PACK, output_dir=directory,
                                   client=client, model='synthetic-test-model', policy=policy)
            self.assertEqual(len(requests), 2)
            payload = json.loads(json.loads(requests[1].content)['input'][1]['content'])
            self.assertEqual(payload['shared_policy'], policy.model_dump())
            manifest = json.loads(Path(summary['validation_manifest_path']).read_text())
            reviewed = json.loads(Path(manifest['results'][0]['path']).read_text())
            self.assertEqual(reviewed['validation']['status'], 'validated')
            self.assertEqual(reviewed['shared_policy'], payload['shared_policy'])
            self.assertEqual(reviewed['preparation']['policy_sha256'], summary['policy_sha256'])
            self.assertEqual(reviewed['validation']['calculations']['vat_rate'], '0.05')
            self.assertIn('VAT (5%)', Path(summary['html_path']).read_text())

    def test_invalid_cli_policy_is_rejected_before_any_network_client(self):
        from meridian.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.json'
            path.write_text('{}')
            with patch('sys.argv', ['meridian', '--run', '--policy', str(path)]), \
                    patch('meridian.llm.create_client') as create_client, \
                    patch('sys.stderr'), self.assertRaises(SystemExit):
                main()
            create_client.assert_not_called()
