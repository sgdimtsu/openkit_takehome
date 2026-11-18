"""Fixed synchronous pipeline; one classifier call, then one call per selected email."""
from pathlib import Path
from time import perf_counter

from .classification_artifacts import save_classification
from .classify import classify_inbox
from .inbox import load_inbox
from .internal import load_internal
from .prepare import prepare_classification
from .render import render_saved_reviews
from .rules import validate_saved_preparations
from .artifacts import save_json_artifact


def run_pipeline(*, input_root, output_dir, client, model, inbox_path='03_inbox_export/inbox.json'):
    started = perf_counter()
    inbox = load_inbox(Path(input_root), inbox_path)
    classification = classify_inbox(inbox, client=client, model=model)
    classified_path = save_classification(classification, output_dir)
    internal = load_internal(Path(input_root))
    preparations = prepare_classification(classification, inbox=inbox, internal=internal,
                                         input_root=input_root, output_dir=output_dir,
                                         client=client, model=model)
    validation = validate_saved_preparations(preparations['manifest_path'], output_dir)
    rendered = render_saved_reviews(validation['manifest_path'], output_dir=output_dir, input_root=input_root)
    classifier_metrics = classification['metadata']['llm']
    classifier_usage = classifier_metrics.get('usage') or {}
    summary = {'classification_path': str(classified_path.resolve()),
               'preparation_manifest_path': preparations['manifest_path'],
               'validation_manifest_path': validation['manifest_path'], 'html_path': rendered['path'],
               'selected_emails': classification['metadata']['total_emails_requiring_action'],
               'preparation_failures': preparations['failed'], 'validation_failures': validation['failed'],
               'calls': {'classification': classifier_metrics['call_count'],
                         'preparation': preparations['call_count'],
                         'total': classifier_metrics['call_count'] + preparations['call_count']},
               'usage_available_for_calls': int(bool(classifier_metrics.get('usage'))) + preparations['usage_available_for_calls'],
               'usage_totals_when_available': {
                   key: classifier_usage.get(key, 0) + preparations['usage_totals_when_available'].get(key, 0)
                   for key in ('input_tokens', 'output_tokens', 'total_tokens')},
               'processing_duration_seconds': perf_counter() - started}
    saved = save_json_artifact(summary, output_dir, prefix='pipeline_run')
    return {**summary, 'manifest_path': str(saved.resolve())}
