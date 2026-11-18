"""Render saved rule-review results as self-contained, retained HTML."""
import hashlib
import html
import json
import os
import re
from collections import OrderedDict
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .sources import resolve_source


ACTION_LABELS = {'new_order_request': 'New', 'amendment_request': 'Amendment',
                 'resend': 'Resend', 'uncertain': 'Uncertain'}


def esc(value):
    return html.escape(str(value), quote=True)


def _anchor(*values):
    return 'ref-' + hashlib.sha256('\0'.join(map(str, values)).encode()).hexdigest()[:24]


def _timestamp(value):
    if not value:
        return 'Unknown freshness'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)):
        return str(value) + ' (date only; time unknown)'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        zone = parsed.tzname() if parsed.utcoffset() is not None else 'timezone unknown'
        return parsed.strftime('%d %b %Y %H:%M:%S') + ' ' + zone
    except ValueError:
        try:
            parsed = datetime.strptime(str(value), '%Y%m%d %H%M%S')
            return parsed.strftime('%d %b %Y %H:%M:%S') + ' (timezone unknown)'
        except ValueError:
            return str(value)


def _human(field):
    labels = {'sku': 'Product code', 'delivery_date': 'Delivery date',
              'requested_delivery_date': 'Requested delivery date', 'delivery_urgency': 'Delivery urgency (for review)', 'line_quantity': 'Line quantity',
              'delivery_destination': 'Delivery destination', 'delivery_instructions': 'Delivery instructions',
              'recorded_account_address': 'Recorded account address — confirm delivery site',
              'line_unit_price': 'Line price', 'line_product': 'Product change'}
    return labels.get(field, str(field).replace('_', ' ').capitalize())


def _quantity(value, unit, total=False):
    unit = str(unit or 'unit unknown')
    if str(value) not in {'1', '1.0'} and unit not in {'each', 'unit unknown'}:
        unit += 'es' if unit.endswith(('s', 'x', 'ch', 'sh')) else 's'
    return f'{value} {unit}' + (' total' if total else '')


def _value(value):
    if value is None:
        return '<span class="muted">Unknown / not available</span>'
    if isinstance(value, dict):
        if 'value' in value:
            return '<span class="copyable" tabindex="0">' + esc(value['value']) + '</span>'
        if value.get('kind') == 'urgency':
            return esc(value['raw'])
        if 'date' in value:
            kind = value.get('kind', 'exact_date')
            text = value.get('raw') or value['date']
            note = {'exact_date': value['date'], 'week_commencing': 'Requested week, not a confirmed delivery date',
                    'before': 'Requested deadline, not a confirmed delivery date',
                    'by': 'Requested deadline, not a confirmed delivery date'}.get(kind, '')
            if 'relative_to' in value:
                note = f"Interpreted as {value['date']} from email received {value['relative_to']}. {note}"
            return f'<span class="copyable" tabindex="0">{esc(text)}</span><div class="cell-note">{esc(note)}</div>'
        return '<br>'.join(f'{esc(_human(key))}: {esc(item)}' for key, item in value.items())
    return '<span class="copyable" tabindex="0">' + esc(value) + '</span>'


def _origin(basis):
    labels = {'explicit_request': 'Customer request · unverified', 'recorded_internal': 'Recorded internal value',
              'inferred_suggestion': 'Inferred suggestion · double-check'}
    if not basis:
        return ''
    css = 'origin inferred' if basis == 'inferred_suggestion' else 'origin'
    return f'<span class="{css}">{esc(labels.get(basis, basis))}</span>'


def _failure(message, filename, email_id=None):
    return {'schema_version': '1.0', 'report_id': uuid4().hex,
            'generated_at': datetime.now(timezone.utc).isoformat(), 'business_rules_applied': False,
            'preparation': {'trigger': {'email_id': email_id or 'Unknown email', 'received_at': None},
                            'classification': {}, 'context': {}, 'error': filename},
            'validation': {'status': 'failed', 'readiness': 'Do not proceed', 'error': message,
                           'entry_section_label': 'Proposed changes — do not apply yet', 'changes_to_apply': []}}


def _check_review(value):
    if not isinstance(value, dict) or value.get('schema_version') != '1.0':
        raise ValueError('Expected a saved deterministic-review JSON (schema 1.0)')
    if not isinstance(value.get('report_id'), str) or not value['report_id'] or not value.get('generated_at'):
        raise ValueError('Review ID/generation timestamp is missing')
    review = value.get('validation', {})
    if not isinstance(review, dict):
        raise ValueError('Review validation section is malformed')
    if review.get('status') not in {'validated', 'failed'}:
        raise ValueError('Input has not completed deterministic validation')
    if review.get('readiness') not in {'Checks required', 'Do not proceed'}:
        raise ValueError('Review has no recognised readiness decision')
    if review['status'] == 'validated' and value.get('business_rules_applied') is not True:
        raise ValueError('Business rules have not been applied')
    if review['status'] == 'failed' and review['readiness'] != 'Do not proceed':
        raise ValueError('A failed review cannot permit entry')
    if not isinstance(value.get('preparation'), dict):
        raise ValueError('Review has no preparation record')
    preparation = value['preparation']
    context = preparation.get('context') or {}
    if not isinstance(context, dict) or not isinstance(preparation.get('proposal') or {}, dict):
        raise ValueError('Preparation context/proposal is malformed')
    if not isinstance(context.get('sources', {}), dict) or any(
            not isinstance(source, dict) for source in context.get('sources', {}).values()):
        raise ValueError('Source catalogue is malformed')
    if not isinstance(context.get('customer_record') or {}, dict) or not isinstance(context.get('customer_match') or {}, dict):
        raise ValueError('Customer matching information is malformed')
    return value


def load_reviews(input_path):
    """Accept a review, retained validation manifest or folder of reviewed JSON files."""
    input_path = Path(input_path)
    def read(path, email_id=None):
        try:
            return _check_review(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            return _failure(f'Cannot use reviewed input {path.name}: {exc}', path.name, email_id)
    if input_path.is_dir():
        paths = sorted(input_path.glob('validated_preparation_*.json'))
        if not paths:
            raise ValueError('No validated_preparation_*.json files found in this folder')
        return [read(path) for path in paths]
    try:
        document = json.loads(input_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return [_failure(f'Cannot read reviewed input {input_path.name}: {exc}', input_path.name)]
    if isinstance(document, dict) and 'results' in document and 'validated' in document:
        if not isinstance(document['results'], list):
            return [_failure('Validation manifest results are malformed.', input_path.name)]
        reviews = []
        for item in document['results']:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                reviews.append(_failure('Validation manifest entry has no readable review path.', input_path.name))
            else:
                reviews.append(read(input_path.parent / Path(item['path']).name, item.get('email_id')))
        return reviews
    try:
        return [_check_review(document)]
    except (ValueError, TypeError, AttributeError) as exc:
        return [_failure(str(exc), input_path.name)]


class _Report:
    def __init__(self, document, input_root, output_dir):
        self.doc, self.review = document, document['validation']
        self.prep = document['preparation']
        self.context = self.prep.get('context') or {}
        self.proposal = self.prep.get('proposal') or {}
        self.sources = self.context.get('sources', {})
        self.input_root, self.output_dir = Path(input_root), Path(output_dir)
        self.id = document['report_id']
        self.trigger = self.prep.get('trigger', {})
        self.action = self.proposal.get('action_type') or self.prep.get('classification', {}).get('action_type', 'uncertain')
        self.account = self.context.get('customer_record') or {}
        self.match = self.context.get('customer_match') or {}
        self.matched = self.review['status'] == 'validated' and self.match.get('status') == 'matched' and bool(self.account)
        self.matched_sources = {}
        self.history_sources = {}
        for collection, mapping in ((self.context.get('explicitly_matched_orders', []), self.matched_sources),
                                    (self.context.get('supporting_history', []), self.history_sources)):
            for order in collection:
                for row in [order['header'], *order['lines']]:
                    mapping[row['source_id']] = row

    def label(self, key):
        source = self.sources.get(key)
        if not source:
            return 'Evidence unavailable: ' + str(key)
        if key == self.context.get('current_request', {}).get('source_id'):
            email = self.context['current_request']
            return f"Email {email['id']} · {email['from']} · {email['subject']}"
        if key in self.matched_sources or key in self.history_sources:
            row = self.matched_sources.get(key) or self.history_sources[key]
            kind = 'Matched order' if key in self.matched_sources else 'Supporting history · order'
            return f"{kind} {row['ORDNO']}" + (f" · line {row['LNNO']}" if 'LNNO' in row else '')
        try:
            row = json.loads(source.get('text') or '')
        except (ValueError, TypeError):
            row = None
        if isinstance(row, dict):
            if 'sku' in row:
                return f"Price list · {row['sku']}"
            if 'CUSNO' in row and 'CUSNM' in row:
                return f"Orbit account · {row['CUSNM']} ({row['CUSNO']})"
            if 'STKCD' in row:
                return f"Orbit product record · {row['STKCD']}"
            if 'PKEY' in row:
                return f"Orbit export setting · {row['PKEY']}"
        return source.get('display_name') or source.get('relative_path') or str(key)

    def refs(self, keys):
        if not keys:
            return ''
        links = []
        for key in dict.fromkeys(keys):
            if key in self.sources:
                links.append(f'<a data-evidence href="#{_anchor(self.id, key)}">{esc(self.label(key))}</a>')
            else:
                links.append(f'<span>{esc(self.label(key))}</span>')
        return '<div class="source-refs">' + ''.join(links) + '</div>'

    def section(self, number, title, body):
        return f'<section class="section"><h4><span class="section-number">{number}</span>{esc(title)}</h4>{body}</section>'

    def status(self):
        if self.review['status'] == 'failed':
            return '<p class="muted">Order status could not be established because validation failed.</p>'
        result = []
        for status in self.review.get('current_order_status', []):
            if 'message' in status:
                result.append(f'<p>{esc(status["message"])}</p>')
                continue
            result.append(f'<p><strong>Orbit order {esc(status["orbit_order_number"])}</strong> · recorded status '
                          f'<span class="copyable">{esc(status.get("status_code_raw"))}</span> '
                          f'<span class="muted">({esc(status.get("status_meaning", "Meaning unknown"))})</span><br>'
                          f'<span class="meta">Snapshot: {esc(_timestamp(status.get("snapshot_timestamp_raw")))}</span></p>')
            rows = ''.join(f'<tr><td>{esc(line["sku"])}</td><td>{esc(line["ordered_quantity_raw"])}</td>'
                           f'<td>{esc(line["dispatched_quantity_raw"])}</td><td>{esc(line.get("recorded_unit_raw") or "Unknown")}</td></tr>'
                           for line in status.get('lines', []))
            if rows:
                result.append('<div class="table-wrap"><table class="status"><thead><tr><th>Product</th><th>Ordered</th>'
                              '<th>Dispatched</th><th>Unit (raw)</th></tr></thead><tbody>' + rows + '</tbody></table></div>')
            result.append(self.refs(status.get('evidence_ids', [])))
        return ''.join(result) or '<p>No recorded order status was supplied.</p>'

    def findings(self):
        checks, issues = [], []
        for item in self.review.get('issues', []):
            model = item.get('origin') == 'llm_proposal'
            origin = '<span class="origin inferred">Model-reported issue · verify</span>' if model else ''
            row = f'<li><p class="item-title">{esc(item["description"])}</p>{origin}'
            row += f'<p class="next">{esc(item["next_action"])}</p>'
            if item.get('owner'):
                row += f'<div class="owner">Responsible: {esc(item["owner"])}</div>'
            if item.get('affected_fields'):
                row += '<div class="cell-note">Affects: ' + esc(', '.join(item['affected_fields'])) + '</div>'
            row += self.refs(item.get('evidence_ids', [])) + '</li>'
            (issues if item.get('blocks_entry') or model else checks).append(row)
        for item in self.review.get('uncertainties', []):
            row = f'<li><p class="item-title">{esc(item["question"])}</p><p class="next">{esc(item["next_action"])}</p>'
            if item.get('affected_fields'):
                row += '<div class="cell-note">Affects: ' + esc(', '.join(item['affected_fields'])) + '</div>'
            row += self.refs(item.get('evidence_ids', [])) + '</li>'
            checks.append(row)
        if self.review.get('error'):
            preparation_failed = self.review.get('failure_stage') == 'preparation' or self.prep.get('status') == 'failed'
            title = 'Preparation failed' if preparation_failed else 'Validation failed'
            next_action = ('Retry preparation for this email before review.'
                           if self.review.get('failure_reason') == 'timeout'
                           else 'Resolve the failure and review again before entry.')
            issues.insert(0, f'<li><p class="item-title">{title}</p><p class="next">{esc(self.review["error"])}</p>'
                             f'<p class="next">{next_action}</p></li>')
        return (self.section(2, 'Things to confirm', '<ul class="checks-list">' + ''.join(checks) + '</ul>') if checks else '') + (
            self.section(3, 'Outstanding issues', '<ul class="issues-list">' + ''.join(issues) + '</ul>') if issues else '')

    def line_table(self, lines):
        rows = []
        for line in lines:
            basis = line.get('field_basis', {})
            product = esc(line.get('description') or line.get('requested_description', {}).get('value', 'Product unresolved'))
            product += f'<span class="sku">{esc(line.get("sku") or "SKU unresolved")}</span>' + _origin(line.get('sku_basis') or basis.get('sku'))
            count = (_value(_quantity(line['quantity'], line.get('unit'), True)) if 'quantity' in line
                     else '<span class="muted">Quantity/unit unresolved</span>')
            count += '<br>' + _origin(basis.get('quantity'))
            if basis.get('unit') == 'inferred_suggestion':
                count += '<br>' + _origin(basis['unit'])
            price = line.get('price') or {}
            amount = ('£' + str(price['unit_price_ex_vat']) + ' / ' + str(price['unit'])) if 'unit_price_ex_vat' in price else None
            price_html = ('£' + _value(price['unit_price_ex_vat']) + ' / ' + esc(price['unit'])) if amount is not None else _value(None)
            if price:
                price_html += f'<div class="cell-note">{esc(price["basis"])}. Discount already included; do not apply it again.</div>'
            total = '£' + _value(line['line_net']) if 'line_net' in line else _value(None)
            evidence = line.get('field_evidence')
            if evidence:
                product += self.refs(evidence.get('description', []) + evidence.get('sku', []))
                count += self.refs(evidence.get('quantity', []) + evidence.get('unit', []))
                price_html += self.refs(price.get('evidence_ids', []) + evidence.get('quoted_price', []))
            else:
                product += self.refs(line.get('evidence_ids', []) + price.get('evidence_ids', []))
            rows.append(f'<tr><td>{product}</td><td>{count}</td><td>{price_html}</td><td>{total}</td></tr>')
        if not rows:
            return '<p class="muted">No supported order lines were established.</p>'
        return ('<div class="table-wrap"><table class="lines"><thead><tr><th>Product / SKU</th><th>Total quantity</th>'
                '<th>Unit price ex VAT</th><th>Line net</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')

    def changes(self):
        if self.review['status'] == 'failed':
            return '<p>No validated entry values. Resolve the failure before applying changes.</p>'
        if self.action == 'resend':
            return '<div class="notice critical"><strong>Do not create another order.</strong> Check the original submission and existing Orbit entry.</div>'
        if self.action == 'uncertain':
            return '<p>No entry values can be established until the requested action is clarified.</p>'
        result, amendments = [], []
        for change in self.review.get('changes_to_apply', []):
            if change.get('operation') == 'new_order':
                headers = []
                for field, value in change.get('header', {}).items():
                    headers.append(f'<dt>{esc(_human(field))}</dt><dd>{_value(value)}<br>{_origin(value.get("basis"))}'
                                   + self.refs(value.get('evidence_ids', [])) + '</dd>')
                if headers:
                    result.append('<dl class="field-values">' + ''.join(headers) + '</dl>')
                result.append(self.line_table(change.get('lines', [])))
                continue
            target = 'Orbit ' + str(change.get('orbit_order_number', 'target unresolved'))
            if change.get('sku'):
                target += ' · ' + str(change['sku'])
            label = esc(_human(change.get('field', 'Requested change'))) + f'<div class="cell-note">{esc(target)}</div>'
            if change.get('applicable') is False:
                amendments.append(f'<tr><td>{label}</td><td>Not established</td><td>Manual review required; no applicable entry values supplied.'
                                  + self.refs(change.get('evidence_ids', [])) + '</td></tr>')
                continue
            if change.get('operation') == 'add_line':
                new = self.line_table([change['proposed_value']])
                old = '<span class="muted">Additional line requested</span>'
            elif change.get('field') == 'line_quantity':
                old = _value(_quantity(change.get('old_value'), change.get('unit')))
                new = _value(_quantity(change.get('proposed_value'), change.get('unit'), True))
                if change.get('requested_quantity_basis') == 'additional':
                    new += f'<div class="cell-note">Requested as {esc(change.get("requested_value"))} additional. The displayed replacement is the total.</div>'
                new += f'<div class="cell-note">Recorded net price: £{esc(change.get("recorded_net_unit_price_ex_vat", "Unknown"))} / {esc(change.get("unit", "unit unknown"))}. '
                new += 'Quantity amendment only; no automatic repricing.</div>'
            elif change.get('operation') == 'remove_line':
                old, new = _value(change.get('old_value')), 'Removal requested · confirm before applying'
            else:
                old = _value(change.get('old_value'))
                if change.get('old_value_status'):
                    old += '<div class="cell-note">' + esc(change['old_value_status']) + '</div>'
                new = _value(change.get('proposed_value'))
            old += self.refs(change.get('recorded_evidence_ids', []))
            new += '<div class="cell-note">Unverified customer request · confirm before applying.</div>' + self.refs(change.get('evidence_ids', []))
            amendments.append(f'<tr><td>{label}</td><td>{old}</td><td>{new}</td></tr>')
        if amendments:
            result.append('<div class="table-wrap"><table class="changes"><thead><tr><th>Changed field</th><th>Recorded old value</th>'
                          '<th>Proposed replacement</th></tr></thead><tbody>' + ''.join(amendments) + '</tbody></table></div>')
        totals = self.review.get('calculations')
        if totals:
            if totals.get('complete') is False:
                result.append(f'<div class="notice"><strong>Incomplete calculation.</strong> Known lines only: £{esc(totals["known_lines_net_subtotal"])} ex VAT. '
                              f'{esc(totals["reason"])}</div>')
            else:
                result.append('<div class="totals"><dl><dt>Net</dt><dd>£' + esc(totals['net']) + '</dd><dt>VAT (20%)</dt><dd>£'
                              + esc(totals['vat']) + '</dd><dt class="grand">Gross total</dt><dd class="grand">£'
                              + esc(totals['gross']) + '</dd></dl></div><p class="calculation-note">'
                              + esc(totals.get('basis', '')) + '</p>')
        return ''.join(result) or '<p>No changed entry fields were established. Complete the checks before taking action.</p>'

    def evidence(self):
        entries = []
        for key in sorted(self.sources, key=lambda key: self.label(key).lower()):
            source = self.sources[key]
            relative = source.get('relative_path', '')
            link = ''
            try:
                path = resolve_source(self.input_root, relative)
                if path.is_file() and path.suffix.lower() not in {'.sqlite', '.db'}:
                    uri = quote(os.path.relpath(path, self.output_dir.resolve()), safe='/')
                    page = re.search(r'(?i)\bpage\s+(\d+)', source.get('locator', ''))
                    if path.suffix.lower() == '.pdf' and page:
                        uri += '#page=' + page[1]
                    link = f'<a href="{esc(uri)}" target="_blank" rel="noopener noreferrer">Open original file</a>'
            except (ValueError, OSError, TypeError):
                pass
            meta = f'<div class="source-meta">File: {esc(relative)}<br>Location: {esc(source.get("locator", "Unknown"))}<br>'
            meta += f'Source timestamp: {esc(_timestamp(source.get("timestamp")))}'
            if source.get('extraction_method'):
                meta += '<br>Extraction: ' + esc(source['extraction_method'])
            meta += '</div>'
            if source.get('requires_review') or source.get('extraction_error'):
                meta += '<div class="notice critical">Unverified extraction: check the original page. '
                meta += esc(source.get('extraction_error') or 'OCR may omit or misread details.') + '</div>'
            text = source.get('text')
            request = self.context.get('current_request') or {}
            if key == request.get('source_id') and request.get('subject'):
                meta += '<div class="source-meta">Email subject: ' + esc(request['subject']) + '</div>'
            content = '<pre>' + esc(text) + '</pre>' if text else '<p class="muted">No readable source content was retained.</p>'
            entries.append(f'<details class="evidence-item" id="{_anchor(self.id, key)}"><summary>{esc(self.label(key))}</summary>'
                           f'<div class="evidence-body">{meta}{link}{content}</div></details>')
        interpretation = ''
        if self.proposal.get('summary'):
            interpretation += '<p><strong>Model interpretation — verify:</strong> ' + esc(self.proposal['summary']) + '</p>'
        if self.prep.get('classification', {}).get('reason'):
            interpretation += '<p><strong>Classification reason:</strong> ' + esc(self.prep['classification']['reason']) + '</p>'
        metrics = self.prep.get('llm') or {}
        if metrics:
            interpretation += '<p class="meta">Model: ' + esc(metrics.get('model', 'Unknown')) + ' · Calls: '
            interpretation += esc(metrics.get('call_count', 'Unknown')) + ' · Duration: ' + esc(metrics.get('duration_seconds', 'Unknown')) + ' seconds.</p>'
            if metrics.get('usage'):
                interpretation += '<pre>' + esc(json.dumps(metrics['usage'], indent=2)) + '</pre>'
        if self.context.get('source_limitations'):
            interpretation += '<ul>' + ''.join('<li>' + esc(value) + '</li>' for value in self.context['source_limitations']) + '</ul>'
        body = ('<p class="muted">Preserved preparation context for diagnosing the failure; not validated entry values.</p>'
                if self.review['status'] == 'failed' else '')
        body += '<details class="evidence-group"><summary>View supporting evidence (' + str(len(entries)) + ' sources)</summary>'
        body += ''.join(entries) + '<div class="interpretation">' + interpretation + '</div></details>'
        return body if entries or interpretation else '<p class="muted">No supporting evidence was available for this failure.</p>'

    def render(self):
        po = (self.proposal.get('customer_po') or {}).get('value') or (self.prep.get('classification', {}).get('references') or {}).get('customer_po')
        title = 'PO / reference ' + str(po) if po else 'Customer request · no PO/reference established'
        readiness = self.review['readiness']
        blocked = readiness == 'Do not proceed'
        failed = self.review['status'] == 'failed'
        badge = readiness
        if failed:
            badge = ('Preparation failed' if self.review.get('failure_stage') == 'preparation'
                     or self.prep.get('status') == 'failed' else 'Validation failed')
        account_label = (str(self.account.get('CUSNM', '')) + ' · ' + str(self.account.get('CUSNO', ''))) if self.matched else 'Customer/account unresolved'
        html_parts = [f'<article class="report" id="{_anchor(self.id)}"><header class="report-header"><div><h3>{esc(title)}</h3>'
                      '<p class="meta">' + esc(account_label) + '</p></div>'
                      '<div class="badges"><span class="badge action">' + esc(ACTION_LABELS.get(self.action, 'Uncertain'))
                      + '</span><span class="badge ' + ('blocked' if blocked else 'checks') + '">' + esc(badge) + '</span></div></header>']
        if self.doc.get('review_date'):
            review_date = esc(self.doc['review_date'])
            if self.doc.get('historical_review'):
                html_parts.append(f'<div class="notice preview"><strong>Historical evaluation as of {review_date}.</strong> '
                                  'Revalidate against the current date and Orbit before entry.</div>')
            else:
                html_parts.append(f'<p class="meta">Rules evaluated as of {review_date}</p>')
        if not self.matched:
            candidates = self.match.get('candidate_account_ids') or []
            html_parts.append('<div class="notice critical"><strong>Customer/account not established for validated entry.</strong> '
                              'Confirm the correct account before proceeding.' + (' Candidate accounts (not assigned): ' + esc(', '.join(candidates)) if candidates else '') + '</div>')
        orders = self.context.get('explicitly_matched_orders', []) if self.review['status'] == 'validated' else []
        numbers = ', '.join(str(order['header']['ORDNO']) for order in orders) or 'No existing match established'
        html_parts.append('<div class="report-meta"><div><span class="label">Trigger email</span>' + esc(self.trigger.get('email_id', 'Unknown'))
                          + ' · ' + esc(_timestamp(self.trigger.get('received_at'))) + '</div><div><span class="label">Existing Orbit order</span>'
                          + esc(numbers) + '</div><div class="meta"><span class="label">Report generated</span>' + esc(_timestamp(self.doc.get('generated_at')))
                          + '</div><div class="meta"><span class="label">Report ID</span>' + esc(self.id) + '</div></div>')
        html_parts.append('<div class="notice ' + ('critical' if blocked else '') + '">' +
                          ('Do not proceed. Resolve the failure before attempting entry.' if failed else
                           'Resolve the outstanding restrictions/checks before applying proposed values.' if blocked else
                           'Complete the manual checks below before entering anything in Orbit. This is not automatic approval.') + '</div>')
        html_parts += [self.section(1, 'Current order status', self.status()), self.findings(),
                       self.section(4, self.review.get('entry_section_label', 'Proposed changes — do not apply yet'), self.changes()),
                       self.section(5, 'Evidence', self.evidence()), '</article>']
        return ''.join(html_parts)


SCRIPT = """document.addEventListener('click', function(event) {
  const link = event.target.closest('a[data-evidence]');
  if (!link) return;
  const target = document.getElementById(link.getAttribute('href').slice(1));
  if (!target) return;
  let node = target;
  while (node) { if (node.tagName === 'DETAILS') node.open = true; node = node.parentElement; }
});"""


def render_html(reviews, *, input_root, output_dir, previous_files=(), notice=None):
    reports, seen = [], set()
    for review in reviews:
        _check_review(review)
        if review['report_id'] in seen:
            raise ValueError('Duplicate review ID supplied; choose distinct review artefacts')
        seen.add(review['report_id'])
        reports.append(_Report(review, input_root, output_dir))
    groups = OrderedDict()
    for report in reports:
        key = report.account['CUSNO'] if report.matched else None
        groups.setdefault(key, []).append(report)
    contents, sections = [], []
    for account, members in groups.items():
        name = members[0].account['CUSNM'] if account else 'Customer unresolved / validation failed'
        anchor = _anchor('customer', account)
        label = name + (' · ' + str(account) if account else '')
        contents.append(f'<li><a href="#{anchor}">{esc(label)}<small>{len(members)} separate report(s)</small></a></li>')
        heading = f'<div class="customer-heading"><h2>{esc(name)} <span class="account">{esc(account or "No account assigned")}</span></h2>'
        heading += f'<div class="meta">{len(members)} separate report(s)</div></div>'
        sections.append(f'<section class="customer" id="{anchor}">{heading}' + ''.join(member.render() for member in members) + '</section>')
    now = datetime.now(timezone.utc).isoformat()
    failed = sum(report.review['status'] == 'failed' for report in reports)
    blocked = sum(report.review['status'] == 'validated' and report.review['readiness'] == 'Do not proceed'
                  for report in reports)
    checks = len(reports) - failed - blocked
    css = files('meridian').joinpath('templates/report.css').read_text(encoding='utf-8')
    banner = '<div class="notice preview">' + esc(notice) + '</div>' if notice else ''
    earlier = ''
    if previous_files:
        links = ''.join(f'<li><a href="{esc(quote(Path(path).name, safe=""))}">{esc(Path(path).name)}</a></li>' for path in previous_files)
        earlier = '<details class="previous"><summary>Earlier HTML runs remain available</summary><ul>' + links + '</ul></details>'
    body = ''.join(sections) or '<p class="empty">No reviewed reports were supplied. No order entries are proposed.</p>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; '
            'connect-src \'none\'; base-uri \'none\'; form-action \'none\'">'
            '<title>Meridian · Order preparation review</title><style>' + css + '</style></head><body><div class="page">'
            '<header><div class="eyebrow">Meridian Catering Supplies</div><h1>Order preparation review</h1>'
            '<p class="intro muted">Snapshot inputs · Human checks and manual Orbit entry · Stock and delivery are not guaranteed.</p>'
            '<p class="meta">HTML generated ' + esc(_timestamp(now)) + '. Report and source timestamps are shown separately.</p>'
            + banner + f'<div class="run-stats"><span class="stat">{len(reports)} separate reports</span><span class="stat">{len(groups)} customer groups</span>'
            f'<span class="stat">{blocked} do not proceed</span><span class="stat">{checks} checks required</span>'
            f'<span class="stat">{failed} processing failures</span></div></header><div class="layout"><nav class="contents" aria-label="Customer reports">'
            '<p>Customers</p><ul>' + ''.join(contents) + '</ul></nav><main>' + body + '</main></div><footer class="footer">'
            '<p><strong>Manual review is always required.</strong> A newer report does not prove earlier changes were applied or superseded. '
            'Check current credit status in Orbit; Tom retains credit authority and Karen reviews pricing/delivery exceptions.</p>'
            '<p>Snapshot inputs only; no live connections or detection of later edits. No automatic reconciliation, tracking of Orbit entry, '
            'writes to sources/Orbit, customer email sending or operational database. Recommendations depend on source completeness and correct matching.</p>'
            + earlier + '</footer></div><script>' + SCRIPT + '</script></body></html>')


def render_saved_reviews(input_path, *, output_dir, input_root, notice=None):
    reviews = load_reviews(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = sorted(output_dir.glob('meridian_reports_*.html'), reverse=True)
    text = render_html(reviews, input_root=input_root, output_dir=output_dir,
                       previous_files=previous, notice=notice)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = output_dir / f'meridian_reports_{stamp}_{uuid4().hex}.html'
    with path.open('x', encoding='utf-8') as handle:
        handle.write(text)
    return {'path': str(path.resolve()), 'reports': len(reviews),
            'validation_failures': sum(item['validation']['status'] == 'failed' for item in reviews)}
