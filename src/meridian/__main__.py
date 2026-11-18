"""Inspect local sources or explicitly classify the inbox with OpenAI."""
import argparse
import json
import sys
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
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--pdf", help="Extract one PDF, relative to input root, then exit")
    operation.add_argument("--classify", action="store_true",
                           help="Send inbox text/headers to OpenAI for one classification call")
    operation.add_argument("--classification-schema", action="store_true",
                           help="Print the generated classification JSON Schema without an API call")
    operation.add_argument("--classification-input", type=Path,
                           help="Validate/read a saved classification JSON without an API call")
    operation.add_argument("--prepare", type=Path,
                           help="Read classification JSON and send one preparation request per selected email")
    operation.add_argument("--preparation-schema", action="store_true",
                           help="Print the preparation output schema without an API call")
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="Directory for retained pipeline JSON (default: ./output)")
    parser.add_argument("--model", help="OpenAI model ID; overrides OPENAI_MODEL")
    args = parser.parse_args()
    if args.classification_schema:
        from .schemas import ClassificationOutput
        print(json.dumps(ClassificationOutput.model_json_schema(), indent=2))
        return
    if args.preparation_schema:
        from .preparation_schemas import PreparationOutput
        print(json.dumps(PreparationOutput.model_json_schema(), indent=2))
        return
    if args.pdf:
        from dataclasses import asdict
        from .attachments import extract_pdf
        try:
            result = extract_pdf(args.input_root, args.pdf)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"PDF extraction failed: {exc}\n")
        print(json.dumps({**asdict(result), "all_pages_extracted": result.complete}, indent=2))
        if not result.complete:
            parser.exit(1)
        return
    try:
        loaded = load_inbox(args.input_root, args.inbox)
        internal = load_internal(args.input_root) if args.include_internal or args.prepare else None
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Input loading failed: {exc}\n")
    if args.prepare:
        from .classification_artifacts import load_classification
        from .config import OpenAISettings
        from .llm import create_client
        from .prepare import prepare_classification
        try:
            classification = load_classification(args.prepare, loaded)
            settings = OpenAISettings.from_env(args.model)
            with create_client(settings) as client:
                summary = prepare_classification(classification, inbox=loaded, internal=internal,
                                                 input_root=args.input_root, client=client,
                                                 model=settings.model, output_dir=args.output_dir)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Preparation failed: {exc}\n")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if summary['failed']:
            parser.exit(1)
        return
    if args.classification_input:
        from .classification_artifacts import load_classification
        try:
            result = load_classification(args.classification_input, loaded)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Saved classification loading failed: {exc}\n")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if args.classify:
        from dataclasses import asdict
        from .classify import classify_inbox
        from .config import OpenAISettings
        from .llm import LLMCallError, create_client
        try:
            settings = OpenAISettings.from_env(args.model)
            with create_client(settings) as client:
                result = classify_inbox(loaded, client=client, model=settings.model)
        except ValueError as exc:
            parser.exit(1, f"OpenAI configuration/input error: {exc}\n")
        except LLMCallError as exc:
            print(json.dumps({"status": "failed", "error": str(exc),
                              "metrics": asdict(exc.metrics)}, indent=2))
            parser.exit(1)
        from .classification_artifacts import save_classification
        try:
            path = save_classification(result, args.output_dir)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Classification completed but could not save its JSON: {exc}\n")
        print(f"Classification saved to {path.resolve()}", file=sys.stderr)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
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
