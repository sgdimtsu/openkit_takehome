"""Run-local evidence catalogue. IDs identify locations, not immutable content."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    source_id: str
    relative_path: str
    display_name: str
    locator: str
    timestamp: str | None
    text: str | None = None


def resolve_source(root: Path, relative_path: str) -> Path:
    """Reject paths outside the configured pack, including escaping symlinks."""
    if Path(relative_path).is_absolute():
        raise ValueError(f"Source path must be relative: {relative_path}")
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Source path leaves input root: {relative_path}")
    return path


def add_source(catalogue: dict[str, Source], source: Source) -> None:
    previous = catalogue.get(source.source_id)
    if previous is not None and previous != source:
        raise ValueError(f"Conflicting source ID: {source.source_id}")
    catalogue[source.source_id] = source
