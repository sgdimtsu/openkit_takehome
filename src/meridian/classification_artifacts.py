"""Retained JSON classification output for the next pipeline stage."""
import json
from collections import Counter
from pathlib import Path
from typing import Literal, get_args

from .artifacts import save_json_artifact
from .schemas import ActionType, ClassificationOutput, validate_action_ids


class ClassificationDocument(ClassificationOutput):
    schema_version: Literal['1.0']
    metadata: dict


def save_classification(result, directory):
    document = ClassificationDocument.model_validate(result)
    return save_json_artifact(document.model_dump(), directory, prefix='classification')


def load_classification(path, inbox):
    """Revalidate stored actions against the inbox used by the next stage."""
    document = ClassificationDocument.model_validate(json.loads(Path(path).read_text(encoding='utf-8')))
    email_ids = {email['id'] for email in inbox.emails}
    selected = validate_action_ids(document, email_ids)
    counts = Counter(action.action_type for action in document.actions)
    expected = {
        'total_emails_read': len(inbox.emails),
        'total_emails_requiring_action': len(selected),
        'total_emails_excluded': len(inbox.emails) - len(selected),
        'breakdown_by_action': {kind: counts[kind] for kind in get_args(ActionType)},
    }
    for field, value in expected.items():
        if document.metadata.get(field) != value:
            raise ValueError(f'Saved classification metadata does not match the inbox/actions: {field}')
    return document.model_dump()
