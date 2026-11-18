"""Interpret and retain each selected email separately; no business-rule approval."""
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from tempfile import TemporaryFile
from uuid import uuid4

from .artifacts import save_json_artifact
from .attachments import extract_attachments
from .context import ContextRetriever
from .llm import CallMetrics, LLMCallError, call_structured
from .preparation_schemas import PreparationOutput
from .schemas import ClassificationOutput, validate_action_ids


def _plain(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def validate_preparation(output, context, selection):
    """Validate technical integrity, not the semantic truth of model proposals."""
    if output.email_id != selection['email_id']:
        raise ValueError('Preparation returned a different trigger email ID')
    if output.action_type not in {selection['action_type'], 'uncertain'}:
        raise ValueError('Preparation changed the classified action without marking it uncertain')
    sources = context['sources']
    request = context['current_request']
    attachment_paths = {item['path'] for item in request['attachments']}
    request_ids = {request['source_id']} | {
        key for key, source in sources.items() if source.relative_path in attachment_paths}
    known_skus = {row['sku'] for row in context['catalogue_candidates']}
    known_skus.update(row['STKCD'] for row in context['stock_records'])
    for order in context['supporting_history'] + context['explicitly_matched_orders']:
        known_skus.update(line['STKCD'] for line in order['lines'])

    def check(node):
        if isinstance(node, list):
            for item in node:
                check(item)
        elif isinstance(node, dict):
            if 'evidence_ids' in node:
                ids = node['evidence_ids']
                if len(ids) != len(set(ids)) or any(key not in sources for key in ids):
                    raise ValueError('Preparation cites missing, duplicate or out-of-context evidence IDs')
            if 'basis' in node and 'value' in node:
                ids = node['evidence_ids']
                if not ids or any(not (sources[key].text or '').strip() for key in ids):
                    raise ValueError('Proposed values require readable evidence, not file metadata alone')
                if node['basis'] == 'explicit_request' and not request_ids.intersection(ids):
                    raise ValueError('Explicit requests must cite the trigger email or its attachments')
                if node['basis'] == 'recorded_internal' and not set(ids).difference(request_ids):
                    raise ValueError('Recorded internal values require internal evidence')
                if node['basis'] == 'inferred_suggestion' and not node['reason']:
                    raise ValueError('Inferred suggestions require an explanation')
            if isinstance(node.get('sku'), dict) and node['sku']['value'] not in known_skus:
                raise ValueError('Preparation proposes a SKU absent from supplied catalogue/stock/history')
            for value in node.values():
                check(value)
    check(output.model_dump())
    if output.customer_po and not any(
            ContextRetriever._stated(output.customer_po.value, sources[key].text or '')
            for key in output.customer_po.evidence_ids):
        raise ValueError('Customer PO identifier is not present in its cited evidence')

    def check_quoted_price(price):
        if price is not None and price.amount.basis != 'explicit_request':
            raise ValueError('Customer-quoted amounts must be explicitly requested, not internally inferred')
    lines = []
    if output.new_order:
        lines = output.new_order.lines
        for price in output.new_order.customer_quoted_totals:
            check_quoted_price(price)
        for discount in output.new_order.customer_discounts:
            if discount.description.basis != 'explicit_request':
                raise ValueError('Customer discounts must remain explicit unverified requests')
    if output.amendment:
        changes = output.amendment.changes
        if len({change.change_id for change in changes}) != len(changes):
            raise ValueError('Preparation repeats change IDs')
        orders = {order['header']['ORDNO']: order for order in context['explicitly_matched_orders']}
        for change in changes:
            if change.added_line:
                lines.append(change.added_line)
            if change.field == 'line_product' and change.proposed_value is not None:
                if change.proposed_value.value not in known_skus:
                    raise ValueError('Replacement product is absent from supplied catalogue/stock/history')
            target = change.target
            if target is None:
                continue
            if target.orbit_order_number is not None and target.orbit_order_number not in orders:
                raise ValueError('Amendment target is not an explicitly matched order')
            if target.orbit_line_number is not None:
                order = orders.get(target.orbit_order_number)
                if order is None or target.orbit_line_number not in {line['LNNO'] for line in order['lines']}:
                    raise ValueError('Amendment line target is not in the explicitly matched order')
                line = next(line for line in order['lines'] if line['LNNO'] == target.orbit_line_number)
                if target.sku is not None and target.sku.value != line['STKCD']:
                    raise ValueError('Amendment target SKU conflicts with the recorded order line')
            elif target.sku is not None and target.orbit_order_number is not None:
                if target.sku.value not in {line['STKCD'] for line in orders[target.orbit_order_number]['lines']}:
                    raise ValueError('Amendment target SKU is absent from the matched order')
    if len({line.line_id for line in lines}) != len(lines):
        raise ValueError('Preparation repeats line IDs')
    for line in lines:
        check_quoted_price(line.customer_quoted_price)
    if output.resend:
        allowed = set(selection['related_email_ids'])
        if request.get('in_reply_to'):
            allowed.add(request['in_reply_to'])
        if not set(output.resend.related_email_ids).issubset(allowed):
            raise ValueError('Resend refers to unsupported related email IDs')


def _record(selection, context, *, status, metrics):
    return {'schema_version': '1.0', 'report_id': uuid4().hex,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': status, 'business_rules_applied': False, 'requires_human_review': True,
            'trigger': {'email_id': selection['email_id'],
                        'received_at': context['current_request']['received'] if context else None},
            'classification': selection, 'context': _plain(context) if context else None,
            'report_alerts': _plain(context['report_alerts']) if context else [],
            'llm': asdict(metrics)}


def prepare_email(selection, context, *, client, model):
    if context['email_id'] != selection['email_id']:
        raise ValueError('Context belongs to a different email')
    prompt = files('meridian').joinpath('prompts/prepare.txt').read_text(encoding='utf-8')
    output, metrics = call_structured(client, model=model, prompt=prompt,
                                     payload={'selection': selection, 'context': _plain(context)},
                                     output_type=PreparationOutput)
    try:
        validate_preparation(output, context, selection)
    except ValueError as exc:
        raise LLMCallError(f'Preparation validation failed: {exc}', metrics) from exc
    result = _record(selection, context, status='prepared', metrics=metrics)
    result['proposal'] = output.model_dump(exclude_none=True)
    # Keep missing entry information visible even if the model omits its question.
    missing = []
    if output.new_order:
        if not output.new_order.lines:
            missing.append('order lines')
        for line in output.new_order.lines:
            missing.extend(f'{line.line_id}.{field}' for field in ('sku', 'quantity', 'unit')
                           if getattr(line, field) is None)
    if output.amendment and not context['explicitly_matched_orders']:
        missing.append('existing amendment target')
    if missing:
        result['report_alerts'].append({
            'code': 'entry_details_unresolved', 'title': 'Entry details require confirmation',
            'message': 'Not established: ' + ', '.join(missing), 'blocks_entry': True,
            'placement': 'report_header_and_things_to_confirm'})
    return result


def prepare_classification(classification, *, inbox, internal, input_root, client, model, output_dir):
    """One isolated call and retained result/failure per selected email; no retries."""
    started = perf_counter()
    selected = ClassificationOutput.model_validate({'actions': classification['actions']})
    validate_action_ids(selected, {email['id'] for email in inbox.emails})
    retriever = ContextRetriever(internal, inbox)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryFile(dir=output_dir):
        pass  # Check output is writable before any model call.
    results, metrics_list = [], []
    for selected_action in selected.actions:
        selection = selected_action.model_dump()
        context = None
        try:
            email = retriever.emails[selection['email_id']]
            attachments = extract_attachments(input_root, email)
            context = retriever.prepare(selection, attachment_results=attachments)
            record = prepare_email(selection, context, client=client, model=model)
        except LLMCallError as exc:
            record = _record(selection, context, status='failed', metrics=exc.metrics)
            record['error'] = str(exc)
        except Exception as exc:
            record = _record(selection, context, status='failed', metrics=CallMetrics(0, model))
            record['error'] = f'Context/input preparation failed ({type(exc).__name__}): {exc}'
        record['trigger']['received_at'] = retriever.emails[selection['email_id']]['received']
        metrics_list.append(record['llm'])
        try:
            path = save_json_artifact(record, output_dir, prefix='preparation')
            results.append({'email_id': selection['email_id'], 'report_id': record['report_id'],
                            'status': record['status'], 'path': str(path.resolve())})
        except OSError as exc:
            results.append({'email_id': selection['email_id'], 'status': 'failed',
                            'error': f'Could not retain preparation JSON: {exc}', 'unsaved_result': record})
    counts = Counter(result['status'] for result in results)
    usage = [metrics['usage'] for metrics in metrics_list if metrics['usage'] is not None]
    summary = {'schema_version': '1.0', 'generated_at': datetime.now(timezone.utc).isoformat(),
               'selected_emails': len(selected.actions), 'prepared': counts['prepared'],
               'failed': counts['failed'], 'results': results,
               'call_count': sum(metrics['call_count'] for metrics in metrics_list),
               'usage_available_for_calls': len(usage),
               'usage_totals_when_available': {
                   key: sum(item.get(key, 0) for item in usage)
                   for key in ('input_tokens', 'output_tokens', 'total_tokens')},
               'processing_duration_seconds': perf_counter() - started}
    manifest = save_json_artifact(summary, output_dir, prefix='preparation_run')
    return {**summary, 'manifest_path': str(manifest.resolve())}
