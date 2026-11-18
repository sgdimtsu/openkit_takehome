import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from meridian.classify import classify_inbox
from meridian.config import OpenAISettings
from meridian.inbox import load_inbox
from meridian.llm import LLMCallError, create_client
from meridian.schemas import ClassificationOutput

PACK = Path(os.environ.get('MERIDIAN_TEST_INPUT_ROOT',
                          str(Path(__file__).resolve().parents[1] / 'data')))


def action(email_id='MSG-042', kind='amendment_request'):
    # Synthetic responses exercise plumbing, not real-model classification accuracy.
    return {'email_id': email_id, 'customer': {'name_as_stated': None, 'account_id_as_stated': None},
            'action_type': kind, 'references': {'customer_po': None, 'orbit_order_number': None},
            'related_email_ids': [], 'reason': 'Synthetic test selection.', 'uncertainties': []}


def response_body(actions=None, *, status='completed', refusal=False, text=None):
    content = ({'type': 'refusal', 'refusal': 'Synthetic refusal'} if refusal else
               {'type': 'output_text', 'text': text if text is not None else json.dumps({'actions': actions or []}),
                'annotations': []})
    return {'id': 'resp_synthetic', 'object': 'response', 'created_at': 0,
            'model': 'synthetic-test-model', 'status': status, 'error': None,
            'incomplete_details': {'reason': 'max_output_tokens'} if status == 'incomplete' else None,
            'output': [{'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [content]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}}}


class ClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inbox = load_inbox(PACK)

    def client(self, body, status=200):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(status, json=body)
        client = OpenAI(api_key='synthetic-not-a-real-key', max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        return client, requests

    def test_one_structured_call_and_computed_metadata(self):
        actions = [action(), action('MSG-036', 'resend')]
        actions[1]['related_email_ids'] = ['MSG-011']
        client, requests = self.client(response_body(actions))
        result = classify_inbox(self.inbox, client=client, model='synthetic-test-model')
        self.assertEqual(len(requests), 1)
        request = json.loads(requests[0].content)
        self.assertFalse(request['store'])
        self.assertNotIn('tools', request)
        self.assertTrue(request['text']['format']['strict'])
        self.assertEqual(request['text']['format']['type'], 'json_schema')
        self.assertEqual(result['metadata']['total_emails_read'], len(self.inbox.emails))
        self.assertEqual(result['metadata']['total_emails_requiring_action'], 2)
        self.assertEqual(result['metadata']['total_emails_excluded'], len(self.inbox.emails) - 2)
        self.assertEqual(sum(result['metadata']['breakdown_by_action'].values()), 2)
        self.assertEqual(result['metadata']['llm']['usage']['total_tokens'], 120)
        self.assertEqual(result['metadata']['llm']['call_count'], 1)
        self.assertTrue(result['metadata']['extracted_at'].endswith('+00:00'))
        self.assertGreaterEqual(result['metadata']['llm']['duration_seconds'], 0)
        self.assertEqual(result['actions'], actions)

    def test_only_inbox_data_is_sent_and_untrusted_body_is_not_instructions(self):
        client, requests = self.client(response_body())
        classify_inbox(self.inbox, client=client, model='synthetic-test-model')
        messages = json.loads(requests[0].content)['input']
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertIn('untrusted', messages[0]['content'])
        payload = json.loads(messages[1]['content'])
        self.assertEqual(len(payload['emails']), len(self.inbox.emails))
        for original, supplied in zip(self.inbox.emails, payload['emails']):
            self.assertEqual(original['body'], supplied['body'])
            self.assertEqual(original['id'], supplied['id'])
            self.assertNotIn('source_id', supplied)
            for attachment in supplied['attachments']:
                self.assertEqual(set(attachment), {'filename', 'path'})
        self.assertEqual(set(payload), {'emails'})

    def test_unknown_duplicate_and_invalid_related_ids_fail(self):
        cases = [[action('UNKNOWN')], [action(), action()]]
        for related in [['UNKNOWN'], ['MSG-042'], ['MSG-011', 'MSG-011']]:
            item = action(); item['related_email_ids'] = related; cases.append([item])
        for actions in cases:
            with self.subTest(actions=actions):
                client, requests = self.client(response_body(actions))
                with self.assertRaises(LLMCallError) as caught:
                    classify_inbox(self.inbox, client=client, model='synthetic-test-model')
                self.assertEqual(len(requests), 1)
                self.assertEqual(caught.exception.metrics.call_count, 1)

    def test_schema_rejects_extra_fields_bad_types_and_unknown_action(self):
        for field, value in [('action_type', 'release_credit'), ('email_id', 42),
                             ('reason', ''), ('reason', '   ')]:
            item = action(); item[field] = value
            with self.subTest(field=field, value=value):
                client, requests = self.client(response_body([item]))
                with self.assertRaises(LLMCallError):
                    classify_inbox(self.inbox, client=client, model='synthetic-test-model')
                self.assertEqual(len(requests), 1)
        item = action(); item['requested_changes'] = ['not part of classification']
        client, _ = self.client(response_body([item]))
        with self.assertRaises(LLMCallError):
            classify_inbox(self.inbox, client=client, model='synthetic-test-model')

    def test_refusal_incomplete_and_invalid_json_fail_visibly(self):
        for body in [response_body(refusal=True), response_body(status='incomplete'),
                     response_body(text='not json')]:
            with self.subTest(body=body):
                client, requests = self.client(body)
                with self.assertRaises(LLMCallError) as caught:
                    classify_inbox(self.inbox, client=client, model='synthetic-test-model')
                self.assertEqual(len(requests), 1)
                self.assertGreaterEqual(caught.exception.metrics.duration_seconds, 0)

    def test_provider_error_not_retried_or_replaced_by_success(self):
        client, requests = self.client({'error': {'message': 'Synthetic error', 'type': 'server_error'}}, 500)
        with self.assertRaises(LLMCallError) as caught:
            classify_inbox(self.inbox, client=client, model='synthetic-test-model')
        self.assertEqual(len(requests), 1)
        self.assertIsNone(caught.exception.metrics.usage)
        self.assertNotIn('synthetic-not-a-real-key', str(caught.exception))

    def test_configuration_is_explicit_and_secrets_hidden(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'OPENAI_MODEL'):
                OpenAISettings.from_env()
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                OpenAISettings.from_env('synthetic-test-model')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-secret', 'OPENAI_MODEL': 'env-model'}):
            settings = OpenAISettings.from_env('override-model')
            self.assertEqual(settings.model, 'override-model')
            self.assertNotIn('synthetic-secret', repr(settings))
            with patch('meridian.llm.OpenAI') as constructor:
                create_client(settings)
                self.assertEqual(constructor.call_args.kwargs['max_retries'], 0)

    def test_zero_actions_is_valid_but_no_fabricated_classification(self):
        client, _ = self.client(response_body())
        result = classify_inbox(self.inbox, client=client, model='synthetic-test-model')
        self.assertFalse(result['actions'])
        self.assertEqual(result['metadata']['total_emails_requiring_action'], 0)
        schema = ClassificationOutput.model_json_schema()
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['required'], ['actions'])


if __name__ == '__main__':
    unittest.main()
