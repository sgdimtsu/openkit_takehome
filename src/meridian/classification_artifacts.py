"""Retained JSON classification output for the next pipeline stage."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args
from uuid import uuid4

from .schemas import ActionType, ClassificationOutput, validate_action_ids


class ClassificationDocument(ClassificationOutput):
    schema_version: Literal['1.0']
    metadata: dict


def save_classification(result, directory):
    document = ClassificationDocument.model_validate(result)
    text = json.dumps(document.model_dump(), indent=2, ensure_ascii=False) + '\n'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = directory / f'classification_{stamp}_{uuid4().hex}.json'
    # Exclusive creation protects earlier output, even in the unlikely event of a collision.
    with path.open('x', encoding='utf-8') as handle:
        try:
            handle.write(text)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    return path


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
