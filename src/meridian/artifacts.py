"""Write retained JSON artefacts without overwriting previous runs."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def save_json_artifact(result, directory, *, prefix):
    text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = directory / f'{prefix}_{stamp}_{uuid4().hex}.json'
    with path.open('x', encoding='utf-8') as handle:
        try:
            handle.write(text)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    return path
