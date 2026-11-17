"""Inspect the inbox; no model calls or output files."""
import argparse
import json
from pathlib import Path

from .inbox import load_inbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("data"),
                        help="Input pack directory (default: ./data)")
    parser.add_argument("--inbox", default="03_inbox_export/inbox.json",
                        help="Inbox path relative to input root")
    args = parser.parse_args()
    try:
        loaded = load_inbox(args.input_root, args.inbox)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Inbox loading failed: {exc}\n")
    print(json.dumps({"emails_loaded": len(loaded.emails),
                      "sources_identified": len(loaded.sources),
                      "attachment_references": sum(len(e['attachments']) for e in loaded.emails),
                      "warnings": loaded.warnings}, indent=2))


if __name__ == "__main__":
    main()
