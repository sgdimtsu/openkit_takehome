"""Render saved rule-review results as self-contained, retained HTML."""
from .policy import percent_text
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
              'inferred_suggestion': 'Matched using supplied evidence'}
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

    @property
    def duplicate(self):
        return self.action == 'resend' or any(item.get('code') == 'duplicate_entry_risk'
                                            for item in self.review.get('issues', []))

    @property
    def orders(self):
        return self.context.get('explicitly_matched_orders', []) if self.review['status'] == 'validated' else []

    @property
    def po(self):
        return ((self.proposal.get('customer_po') or {}).get('value')
                or (self.prep.get('classification', {}).get('references') or {}).get('customer_po'))

    def outcome(self):
        if self.review['status'] == 'failed':
            return 'Preparation failed' if self.prep.get('status') == 'failed' else 'Validation failed'
        if self.duplicate:
            return 'Existing order found' if self.orders else 'Locate original order'
        return 'Decision required' if self.review['readiness'] == 'Do not proceed' else 'Prepared for entry'

    def refs(self, keys, hint=None):
        links = []
        for key in dict.fromkeys(keys or []):
            if key not in self.sources:
                links.append('<span class="pending">Evidence unavailable</span>')
                continue
            source = self.sources[key]
            full = self.label(key)
            filename = Path(source.get('relative_path') or '').name
            if key == self.context.get('current_request', {}).get('source_id'):
                label = 'Email ' + str(self.trigger.get('email_id', ''))
            elif key in self.matched_sources:
                row = self.matched_sources[key]
                label = 'Orbit line ' + str(row['LNNO']) if 'LNNO' in row else 'Order ' + str(row['ORDNO'])
            elif filename == 'price_list.csv':
                label = 'Price list'
            elif filename == 'customer_account_notes.md':
                label = 'Account notes'
            elif filename.lower().endswith('.pdf'):
                page = re.search(r'(?i)\bpage\s+(\d+)', source.get('locator', ''))
                label = filename + (' · p' + page[1] if page else '')
            else:
                label = full
            excerpt = ''
            if hint:
                lines = (source.get('text') or '').splitlines()
                matches = [line for line in lines if str(hint).casefold() in line.casefold()]
                excerpt = '\n'.join(matches)
            links.append(f'<a class="evidence" data-evidence data-excerpt="{esc(excerpt)}" '
                         f'href="#{_anchor(self.id, key)}" title="{esc(full)}">{esc(label)} ↗</a>')
        return '<div class="source-refs">' + ''.join(links) + '</div>' if links else ''

    def identity(self):
        if not self.matched:
            return ('<div class="record"><div><span class="label">Customer/account unresolved</span>'
                    '<b>No account assigned</b><p>Customer/account not established for entry.</p></div>'
                    '<div><span class="label">Request</span>' + esc(self.trigger.get('email_id', 'Unknown email')) + '</div></div>')
        number = ', '.join(str(order['header']['ORDNO']) for order in self.orders)
        if number and (self.duplicate or self.action != 'new_order_request'):
            label = 'Open existing Orbit order' if self.duplicate else 'Open Orbit order'
        else:
            label, number = 'Start in Orbit · account', self.account['CUSNO']
        reference = ('<div><span class="label">Customer PO / reference</span><b class="copyable">'
                     + esc(self.po) + '</b></div>') if self.po else ''
        return (f'<div class="record"><div><span class="label">{esc(label)}</span><strong class="record-number copyable">{esc(number)}</strong></div>'
                '<div><span class="label">Account</span><b>' + esc(self.account['CUSNO']) + '</b><span>'
                + esc(self.account['CUSNM']) + '</span></div>' + reference + '</div>')

    def status(self):
        """Only relevant recorded facts for existing-order actions."""
        if not self.orders:
            return ''
        targets = {item.get('sku') for item in self.review.get('changes_to_apply', []) if item.get('sku')}
        facts = []
        for status in self.review.get('current_order_status', []):
            if 'orbit_order_number' not in status:
                continue
            detail = f'Order {status["orbit_order_number"]} · recorded status {status.get("status_code_raw", "unknown")} (meaning not documented)'
            facts.append('<span>' + esc(detail) + self.refs(status.get('evidence_ids')) + '</span>')
            for line in status.get('lines', []):
                if line.get('sku') in targets:
                    facts.append('<span>' + esc(f'{line["sku"]}: {line["ordered_quantity_raw"]} ordered · {line["dispatched_quantity_raw"]} dispatched')
                                 + self.refs(line.get('evidence_ids')) + '</span>')
        return '<div class="order-facts"><b>Recorded position</b>' + ''.join(facts) + '</div>' if facts else ''

    def findings(self):
        ignored = {'manual_credit_check', 'snapshot_limitations', 'vat_conflict', 'vat_unknown',
                   'inferred_product', 'existing_order_not_established', 'recorded_contract_reference'}
        if self.duplicate:
            ignored.update({'duplicate_entry_risk', 'resend'})
        rows, seen = [], set()
        for item in [*self.review.get('issues', []), *self.review.get('uncertainties', [])]:
            if item.get('code') in ignored:
                continue
            question = item.get('question') or item.get('description', '')
            action = item.get('next_action', '')
            if (question, action) in seen:
                continue
            seen.add((question, action))
            owner = '<b>' + esc(item['owner']) + '</b> · ' if item.get('owner') else ''
            rows.append('<li><h4>' + esc(question) + '</h4><p>' + owner + esc(action) + '</p>'
                        + self.refs(item.get('evidence_ids', [])) + '</li>')
        if not rows:
            return ''
        return ('<section class="decision"><div class="section-heading"><span class="step">1</span>'
                '<h3>Before entry</h3></div><ul class="decisions">' + ''.join(rows) + '</ul></section>')

    def date_pending(self):
        return (any(item.get('blocks_entry') and item.get('code') in {'date_unresolved', 'past_requested_date'}
                    for item in self.review.get('issues', []))
                or any(item.get('blocks_entry') and any('date' in field for field in item.get('affected_fields', []))
                       for item in self.review.get('uncertainties', [])))

    def line_table(self, lines):
        rows = []
        for line in lines:
            sku = line.get('sku')
            description = line.get('description') or line.get('requested_description', {}).get('value', 'Product unresolved')
            field = line.get('field_evidence', {})
            product = '<b class="sku copyable">' + esc(sku or 'Product code needed') + '</b><span class="description">' + esc(description) + '</span>'
            product += self.refs(field.get('sku', []) + field.get('description', []) or line.get('evidence_ids', []), sku)
            qty = _value(line.get('quantity'))
            qty += self.refs(field.get('quantity', []), sku)
            unit = esc(line.get('unit') or 'Unit needed') + self.refs(field.get('unit', []), sku)
            price = line.get('price') or {}
            quoted = field.get('quoted_price', [])
            price_html = ('£' + _value(price['unit_price_ex_vat'])) if 'unit_price_ex_vat' in price else '<span class="pending">Price needed</span>'
            price_html += self.refs(price.get('evidence_ids', []) + quoted, sku)
            net = '£' + esc(line['line_net']) if 'line_net' in line else '—'
            rows.append(f'<tr><td>{product}</td><td class="number">{qty}</td><td>{unit}</td><td class="number">{price_html}</td><td class="number">{net}</td></tr>')
        if not rows:
            return '<p class="pending">No supported order lines were established.</p>'
        return ('<div class="table-wrap"><table class="lines"><thead><tr><th>Product code &amp; description</th>'
                '<th class="number">Qty</th><th>Sales unit</th><th class="number">Unit ex VAT</th><th class="number">Line net</th>'
                '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')

    def totals(self):
        totals = self.review.get('calculations')
        if not totals:
            return ''
        if totals.get('complete') is False:
            return ('<div class="completed"><b>Partial calculation</b><span>Known lines: £'
                    + esc(totals['known_lines_net_subtotal']) + ' ex VAT. No complete order total is available.</span></div>')
        return ('<dl class="totals"><dt>Net</dt><dd>£' + esc(totals['net']) + '</dd><dt>VAT ('
                + esc(percent_text(totals['vat_rate'])) + '%)</dt><dd>£' + esc(totals['vat'])
                + '</dd><dt class="grand">Order total</dt><dd class="grand">£' + esc(totals['gross']) + '</dd></dl>')

    def new_order(self):
        changes = [item for item in self.review.get('changes_to_apply', []) if item.get('operation') == 'new_order']
        if not changes:
            return '<p class="pending">No prepared order fields were established.</p>'
        parts = []
        for change in changes:
            fields = []
            if self.po:
                fields.append('<div><span class="label">Customer PO / reference</span>' + _value(self.po) + '</div>')
            for name, value in change.get('header', {}).items():
                shown = _value(value)
                if name == 'requested_delivery_date' and self.date_pending():
                    shown = '<b class="pending">Awaiting revised or confirmed date</b><span class="description">Requested: ' + esc(value.get('raw') or value.get('value', 'Unknown')) + '</span>'
                fields.append('<div class="' + ('wide' if 'destination' in name or 'address' in name else '') + '"><span class="label">'
                              + esc(_human(name)) + '</span>' + shown + self.refs(value.get('evidence_ids')) + '</div>')
            parts.append('<div class="fields">' + ''.join(fields) + '</div>' + self.line_table(change.get('lines', [])))
            bases = list(dict.fromkeys(line['price']['basis'] for line in change.get('lines', []) if line.get('price')))
            matched = bool(change.get('lines')) and all(line.get('quoted_price_matches') for line in change['lines'])
            pricing = '<div><span class="label">Pricing prepared</span><p>' + '<br>'.join(esc(basis) for basis in bases)
            if matched:
                pricing += '<br><b>All quoted line prices match.</b>'
            pricing += '<br>Prepared prices are net; do not apply another band/contract discount.</p></div>' if bases else '</p></div>'
            parts.append('<div class="pricing-footer">' + (pricing if bases else '') + self.totals() + '</div>')
        return ''.join(parts)

    def amendments(self):
        rows, retained_prices = [], []
        for change in self.review.get('changes_to_apply', []):
            label = _human(change.get('field', 'Requested change'))
            if change.get('orbit_line_number') is not None:
                label = 'Line ' + str(change['orbit_line_number']) + ' · ' + label
            field = '<b>' + esc(label) + '</b>'
            if change.get('sku'):
                field += '<span class="description">' + esc(change['sku']) + '</span>'
            if change.get('applicable') is False:
                old, proposed = 'Not established', '<b class="pending">Target or entry decision needed</b>'
            elif change.get('operation') == 'add_line':
                old, proposed = 'Additional line', self.line_table([change['proposed_value']])
            elif change.get('field') == 'line_quantity':
                old = _value(_quantity(change.get('old_value'), change.get('unit')))
                proposed = _value(_quantity(change.get('proposed_value'), change.get('unit'), True))
                if change.get('requested_quantity_basis') == 'additional':
                    proposed += '<span class="description">Customer requested ' + esc(change.get('requested_value')) + ' additional.</span>'
                if change.get('recorded_net_unit_price_ex_vat'):
                    retained_prices.append(esc(change.get('sku')) + ': £' + esc(change['recorded_net_unit_price_ex_vat'])
                                           + ' net per ' + esc(change.get('unit')) + '; revised line net £' + esc(change['proposed_line_net']))
            elif change.get('operation') == 'remove_line':
                old, proposed = _value(change.get('old_value')), 'Remove the identified line'
            else:
                old = _value(change.get('old_value'))
                if change.get('old_value_status'):
                    old += '<span class="description">' + esc(change['old_value_status']) + '</span>'
                proposed = _value(change.get('proposed_value'))
                if change.get('field') == 'delivery_date' and self.date_pending():
                    value = change.get('proposed_value') or {}
                    requested = value.get('raw') or value.get('value') if isinstance(value, dict) else value
                    proposed = '<b class="pending">Awaiting revised or confirmed date</b><span class="description">Requested: ' + esc(requested) + '</span>'
            rows.append('<tr><td>' + field + '</td><td>' + old + self.refs(change.get('recorded_evidence_ids'))
                        + '</td><td class="replacement">' + proposed + '</td><td>' + self.refs(change.get('evidence_ids'), change.get('sku')) + '</td></tr>')
        if not rows:
            return '<p class="pending">No supported amendment fields were established.</p>'
        body = ('<div class="table-wrap"><table class="changes"><thead><tr><th>Orbit field / line</th><th>Recorded value</th>'
                '<th>Replacement to enter</th><th>Evidence</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
        if retained_prices:
            body += '<div class="completed"><b>Price retained</b>' + ''.join('<span>' + price + '</span>' for price in retained_prices)
            body += '<span>Quantity change only. No repricing or extra band discount.</span></div>'
        return body

    def failure(self):
        timeout = self.review.get('failure_reason') == 'timeout' or 'APITimeoutError' in self.review.get('error', '')
        action = 'Retry preparation for this email.' if timeout else 'Correct the preparation/validation error, then prepare this email again.'
        return ('<section class="decision"><h3>' + esc(self.outcome()) + '</h3><p>No validated entry values. Do not proceed with entry.</p>'
                '<p><b>Recovery action:</b> ' + esc(action) + '</p><details class="diagnostic"><summary>Error detail</summary><pre>'
                + esc(self.review.get('error', 'Preparation failed.')) + '</pre></details></section>')

    def render(self):
        failed = self.review['status'] == 'failed'
        name = self.account.get('CUSNM') if self.matched else (self.prep.get('classification', {}).get('customer') or {}).get('name_as_stated') or 'Customer/account unresolved'
        action = 'RESEND / DUPLICATE' if self.duplicate else {'new_order_request': 'NEW ORDER', 'amendment_request': 'AMEND EXISTING ORDER', 'uncertain': 'RESOLVE REQUEST'}.get(self.action, 'RESOLVE REQUEST')
        intro = ('No new order entry is required.' if self.duplicate and self.orders else
                 'Review the requested changes on the existing order.' if self.action == 'amendment_request' else
                 'Prepare a new order on this customer account.' if self.action == 'new_order_request' else
                 'Establish the required action before entry.')
        colour = 'green' if self.duplicate and self.orders or self.review['readiness'] != 'Do not proceed' else 'amber'
        body = [f'<article class="report" id="{_anchor(self.id)}"><header class="report-top"><div><span class="kicker">{esc(action)}</span>'
                f'<h2>{esc(name)}</h2><p>{esc(intro)}</p></div><span class="badge {colour}">{esc(self.outcome())}</span></header>', self.identity()]
        body.append('<p class="request-meta">Email ' + esc(self.trigger.get('email_id', 'Unknown')) + ' · Received ' + esc(_timestamp(self.trigger.get('received_at'))) + '</p>')
        if self.doc.get('historical_review'):
            body.append('<div class="notice preview">Historical evaluation as of ' + esc(self.doc.get('review_date')) + '. Revalidate before current entry.</div>')
        if failed:
            body.append(self.failure())
        else:
            if self.duplicate or self.action in {'amendment_request', 'uncertain'}:
                body.append(self.status())
            body.append(self.findings())
            if self.duplicate:
                numbers = ', '.join(str(order['header']['ORDNO']) for order in self.orders)
                next_action = ('Open existing Orbit order ' + numbers + ' for this request.' if numbers else 'Locate the original order before taking action.')
                body.append('<section class="disposition"><h3>Do not create another order.</h3><p><b>Priya’s action:</b> ' + esc(next_action)
                            + ' There are no new order lines to enter.</p>' + self.refs([order['header']['source_id'] for order in self.orders]) + '</section>')
            elif self.action in {'new_order_request', 'amendment_request'}:
                title = 'Fields for the new order' if self.action == 'new_order_request' else 'Changes prepared for the existing order'
                instruction = ('Resolve the decisions above before applying these prepared values.' if self.review['readiness'] == 'Do not proceed'
                               else 'Prepared values for manual entry into Orbit.')
                body.append('<section class="entry"><div class="section-heading"><span class="step">2</span><div><h3>' + title + '</h3><p>'
                            + instruction + '</p></div></div>' + (self.new_order() if self.action == 'new_order_request' else self.amendments()) + '</section>')
            else:
                body.append('<p class="pending">No order-entry fields are proposed until the requested action is resolved.</p>')
        body += [self.evidence(), '</article>']
        return ''.join(body)

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
        if not entries:
            return ''
        return ('<details class="evidence-group"><summary>Source material (' + str(len(entries)) + ' sources)</summary>'
                + ''.join(entries) + '</details>')


SCRIPT = """const panels = [...document.querySelectorAll('article.report')];
const navigation = [...document.querySelectorAll('a[data-panel]')];
function selectPanel(id) {
  if (!panels.some(panel => panel.id === id)) return;
  panels.forEach(panel => panel.hidden = panel.id !== id);
  navigation.forEach(link => {
    if (link.dataset.panel === id) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}
const dialog = document.getElementById('source-dialog');
let evidenceTrigger;
document.addEventListener('click', event => {
  const source = event.target.closest('a[data-evidence]');
  if (source) {
    const target = document.getElementById(source.getAttribute('href').slice(1));
    const body = target && target.querySelector('.evidence-body');
    if (!body) return;
    event.preventDefault();
    evidenceTrigger = source;
    document.getElementById('source-title').textContent = target.querySelector('summary').textContent;
    const content = body.cloneNode(true);
    const text = content.querySelector('pre');
    if (text && source.dataset.excerpt) {
      const full = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Full source text';
      const original = text.cloneNode(true);
      full.append(summary, original);
      text.textContent = source.dataset.excerpt;
      content.append(full);
    }
    document.getElementById('source-content').replaceChildren(content);
    dialog.showModal();
    return;
  }
  const link = event.target.closest('a[data-panel]');
  if (link) {
    event.preventDefault();
    selectPanel(link.dataset.panel);
    history.replaceState(null, '', link.getAttribute('href'));
  }
});
dialog.querySelector('.close').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => { if (evidenceTrigger) evidenceTrigger.focus(); });
if (panels.length) selectPanel(panels.some(panel => '#' + panel.id === location.hash) ? location.hash.slice(1) : panels[0].id);
"""


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
        account = report.account['CUSNO'] if report.matched else None
        groups.setdefault(account, []).append(report)
    contents, sections = [], []
    for account, members in groups.items():
        name = members[0].account['CUSNM'] if account else 'Customer unresolved / processing failures'
        buttons = []
        for report in members:
            anchor = _anchor(report.id)
            reference = str(report.po or report.trigger.get('email_id', 'Customer request'))
            action = 'Duplicate / resend' if report.duplicate else ACTION_LABELS.get(report.action, 'Uncertain')
            buttons.append(f'<a class="nav" href="#{anchor}" data-panel="{anchor}"><b>{esc(reference)}</b>'
                           f'<span>{esc(action)} · {esc(report.trigger.get("email_id", "Unknown email"))}</span>'
                           f'<small>{esc(report.outcome())}</small></a>')
        contents.append('<div class="nav-group"><h2>' + esc(name) + '</h2>' + ''.join(buttons) + '</div>')
        sections.append('<section class="customer">' + ''.join(report.render() for report in members) + '</section>')
    css = files('meridian').joinpath('templates/report.css').read_text(encoding='utf-8')
    banner = '<div class="preview">' + esc(notice) + '</div>' if notice else ''
    failed = sum(report.review['status'] == 'failed' for report in reports)
    duplicates = sum(report.duplicate and report.review['status'] == 'validated' for report in reports)
    earlier = ''
    if previous_files:
        links = ''.join(f'<li><a href="{esc(quote(Path(path).name, safe=""))}">Earlier report {index}</a></li>'
                        for index, path in enumerate(previous_files, 1))
        earlier = '<details class="previous"><summary>Previous reports</summary><ul>' + links + '</ul></details>'
    body = ''.join(sections) or '<p class="empty">No reviewed reports were supplied. No order entries are proposed.</p>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; '
            'script-src \'unsafe-inline\'; connect-src \'none\'; base-uri \'none\'; form-action \'none\'">'
            '<title>Meridian · Order preparation</title><style>' + css + '</style></head><body>' + banner
            + '<div class="page"><header class="masthead"><div><div class="eyebrow">MERIDIAN CATERING SUPPLIES</div>'
            '<h1>Order preparation</h1><p class="sub">Prepared details and remaining decisions for manual entry into Orbit.</p></div>'
            f'<div class="run-stats">{len(reports)} reports · {duplicates} duplicate/resend · {failed} processing failures</div></header>'
            '<div class="layout"><nav class="sidebar" aria-label="Customer reports">' + ''.join(contents) + '</nav><main>' + body
            + '</main></div><footer class="endnote">Prepared for manual entry; no Orbit records have been changed.' + earlier
            + '</footer></div><dialog id="source-dialog" aria-labelledby="source-title"><button class="close" aria-label="Close evidence">×</button>'
            '<span class="kicker">SUPPORTING EVIDENCE</span><h2 id="source-title"></h2><div id="source-content"></div></dialog>'
            '<script>' + SCRIPT + '</script></body></html>')


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
