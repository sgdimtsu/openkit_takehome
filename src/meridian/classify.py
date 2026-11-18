"""Classify the inbox once, validate IDs, then calculate summary metadata in code."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.resources import files
from time import perf_counter
from typing import get_args

from .llm import LLMCallError, call_structured
from .schemas import ActionType, ClassificationOutput


def classify_inbox(inbox, *, client, model):
    started = perf_counter()
    email_ids = {email['id'] for email in inbox.emails}
    if len(email_ids) != len(inbox.emails):
        raise ValueError('Inbox contains duplicate email IDs')
    # Only inbox data is sent. No Orbit, references, PDF bytes or local source catalogue.
    emails = [{key: email[key] for key in
               ('id', 'received', 'from', 'to', 'subject', 'body', 'in_reply_to')}
              | {'attachments': [{'filename': attachment['filename'], 'path': attachment['path']}
                                 for attachment in email['attachments']]}
              for email in inbox.emails]
    prompt = files('meridian').joinpath('prompts/classify.txt').read_text(encoding='utf-8')
    output, metrics = call_structured(client, model=model, prompt=prompt,
                                     payload={'emails': emails}, output_type=ClassificationOutput)
    selected = set()
    for action in output.actions:
        if action.email_id not in email_ids:
            raise LLMCallError(f'Classification references an unknown email ID: {action.email_id}', metrics)
        if action.email_id in selected:
            raise LLMCallError(f'Classification repeats email ID: {action.email_id}', metrics)
        selected.add(action.email_id)
        if (len(action.related_email_ids) != len(set(action.related_email_ids)) or
                any(related not in email_ids or related == action.email_id
                    for related in action.related_email_ids)):
            raise LLMCallError(f'Classification has invalid related email IDs for {action.email_id}', metrics)
    counts = Counter(action.action_type for action in output.actions)
    return {
        'schema_version': '1.0',
        'metadata': {
            'extracted_at': datetime.now(timezone.utc).isoformat(),
            'total_emails_read': len(emails),
            'total_emails_requiring_action': len(selected),
            'total_emails_excluded': len(emails) - len(selected),
            'breakdown_by_action': {kind: counts[kind] for kind in get_args(ActionType)},
            'processing_duration_seconds': perf_counter() - started,
            'llm': asdict(metrics),
        },
        'actions': [action.model_dump() for action in output.actions],
    }
