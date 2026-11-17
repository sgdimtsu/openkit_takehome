"""Load inbox records without interpreting customer requests or attachment content."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .sources import Source, add_source, resolve_source


@dataclass
class LoadedInbox:
    emails: list[dict]
    sources: dict[str, Source]
    warnings: list[str]


def _string(record: dict, key: str, where: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string")
    return value


def load_inbox(root: Path, inbox_path: str = "03_inbox_export/inbox.json") -> LoadedInbox:
    root = Path(root).resolve()
    path = resolve_source(root, inbox_path)
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Inbox must be a JSON array")
    sources: dict[str, Source] = {}
    warnings: list[str] = []
    emails: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        where = f"Inbox record {index + 1}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected an object")
        email = dict(raw)
        identifier = raw.get("id")
        if identifier is None or identifier == "":
            canonical = json.dumps({k: v for k, v in raw.items() if k != "id"},
                                   sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            identifier = "generated-" + hashlib.sha256(canonical.encode()).hexdigest()
            warnings.append(f"{where}: assigned missing email ID {identifier}")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"{where}: id must be a nonempty string")
        if identifier in seen:
            raise ValueError(f"{where}: duplicate email ID {identifier}")
        seen.add(identifier)
        email["id"] = identifier
        for key in ("received", "from", "to", "subject", "body"):
            _string(raw, key, where)
        try:
            received = datetime.fromisoformat(raw["received"])
        except ValueError as exc:
            raise ValueError(f"{where}: invalid received timestamp") from exc
        if received.utcoffset() is None:
            raise ValueError(f"{where}: received timestamp requires a timezone")
        reply = raw.get("in_reply_to")
        if reply is not None and (not isinstance(reply, str) or not reply.strip()):
            raise ValueError(f"{where}: in_reply_to must be a nonempty string or null")
        attachments = raw.get("attachments")
        if not isinstance(attachments, list):
            raise ValueError(f"{where}: attachments must be an array")
        source_id = f"email:{quote(identifier, safe='')}:body"
        email["source_id"] = source_id
        label = f"{identifier} — {raw['from']} — {raw['subject']}"
        add_source(sources, Source(source_id, path.relative_to(root).as_posix(), label,
                                  f"JSON record {index + 1}, body", raw["received"], raw["body"]))
        loaded_attachments = []
        for number, attachment in enumerate(attachments, 1):
            loc = f"{where}, attachment {number}"
            if not isinstance(attachment, dict):
                raise ValueError(f"{loc}: expected an object")
            filename = _string(attachment, "filename", loc)
            relative = _string(attachment, "path", loc)
            if not filename.strip() or not relative.strip():
                raise ValueError(f"{loc}: filename and path must be nonempty")
            attachment_path = resolve_source(root, relative)
            relative = attachment_path.relative_to(root).as_posix()
            attachment_id = f"file:{quote(relative, safe='/')}"
            add_source(sources, Source(attachment_id, relative, filename, "Whole file", None))
            available = attachment_path.is_file()
            if not available:
                warnings.append(f"{identifier}: attachment missing: {relative}")
            loaded_attachments.append({**attachment, "source_id": attachment_id,
                                       "available": available})
        email["attachments"] = loaded_attachments
        emails.append(email)
    for email in emails:
        reply = email.get("in_reply_to")
        if reply and reply not in seen:
            warnings.append(f"{email['id']}: related email not in inbox: {reply}")
    return LoadedInbox(emails, sources, warnings)
