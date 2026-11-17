"""Inspect inbox and optionally internal snapshots; no model calls or output files."""
import argparse
import json
from pathlib import Path

from .inbox import load_inbox
from .internal import load_internal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("data"),
                        help="Input pack directory (default: ./data)")
    parser.add_argument("--inbox", default="03_inbox_export/inbox.json",
                        help="Inbox path relative to input root")
    parser.add_argument("--include-internal", action="store_true",
                        help="Also load reference files, CSVs and read-only Orbit snapshot")
    args = parser.parse_args()
    try:
        loaded = load_inbox(args.input_root, args.inbox)
        internal = load_internal(args.input_root) if args.include_internal else None
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Input loading failed: {exc}\n")
    summary = {"emails_loaded": len(loaded.emails),
                      "sources_identified": len(loaded.sources),
                      "attachment_references": sum(len(e['attachments']) for e in loaded.emails),
                      "warnings": loaded.warnings}
    if internal is not None:
        summary["internal"] = {
            "customers": len(internal.customers_by_id),
            "stock_items": len(internal.stock_by_sku),
            "orders": len(internal.orders_by_id),
            "order_lines": sum(map(len, internal.lines_by_order.values())),
            "open_items": len(internal.open_items),
            "price_records": len(internal.prices_by_sku),
            "sources_identified": len(internal.sources),
            "snapshot_timestamp_raw": internal.parameters.get("LASTEXP", {}).get("PVAL"),
            "warnings": internal.warnings,
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
