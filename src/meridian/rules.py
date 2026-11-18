"""Deterministic review of saved proposals. Never approve live credit or stock."""
import copy
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .artifacts import save_json_artifact
from .context import order_date, reference_key
from .normalise import (VAT_RATE, canonical_unit, decimal_number, discount_rate,
                        exclusive_amount, money, money_text, orbit_net_price, quantity, requested_date)
from .preparation_schemas import PreparationOutput
from .prepare import validate_preparation
from .pricing import PricingRules
from .sources import Source


class _Review:
    def __init__(self, context, proposal, today):
        self.context, self.proposal, self.today = context, proposal, today
        self.account = context['customer_record']
        self.pricing = PricingRules(context)
        self.catalogue = self.pricing.catalogue
        self.issues, self.changes, self.lines = [], [], []
        self.totals = None
        self.request_date = datetime.fromisoformat(context['current_request']['received']).date()
        for row in ([self.account] if self.account else []) + context['catalogue_candidates'] + context['stock_records'] + context['open_items']:
            self.check_record(row)
        for order in context['explicitly_matched_orders']:
            self.check_record(order['header'])
            for line in order['lines']:
                self.check_record(line)
        for row in context.get('export_parameters', {}).values():
            self.check_record(row)

    def check_record(self, row):
        key = row['source_id']
        if key not in self.context['sources']:
            raise ValueError('Internal record has no source evidence')
        original = json.loads(self.context['sources'][key].text)
        if any(original.get(name) != value for name, value in row.items() if name != 'source_id'):
            raise ValueError('Internal record disagrees with its preserved source evidence')

    def issue(self, code, description, next_action, *, owner='Order processor', block=False, evidence=(), origin='deterministic_rule'):
        ids = list(dict.fromkeys(evidence))
        if any(key not in self.context['sources'] for key in ids):
            raise ValueError(f'Rule {code} references unavailable source evidence')
        item = {'code': code, 'description': description, 'next_action': next_action,
                'owner': owner, 'blocks_entry': block, 'evidence_ids': ids, 'origin': origin}
        if item not in self.issues:
            self.issues.append(item)

    def baseline(self):
        context, proposal = self.context, self.proposal
        for alert in context['report_alerts']:
            self.issue(alert['code'], alert.get('message', alert['title']),
                       'Resolve the highlighted check before applying affected values.',
                       block=alert['blocks_entry'])
        for source in context['sources'].values():
            if source.extraction_error or source.requires_review:
                self.issue('unverified_attachment', source.extraction_error or 'OCR page requires manual verification.',
                           'Compare all requested details with the original page.', block=True,
                           evidence=[source.source_id])
        if context['customer_match']['status'] != 'matched' or not self.account:
            self.issue('customer_unresolved', 'No reliable customer account has been established.',
                       'Confirm the correct account; do not use another account’s history.', block=True)
        elif context['customer_match']['account_id'] != self.account['CUSNO']:
            raise ValueError('Matched account ID conflicts with customer record')
        account_ids = [self.account['source_id']] if self.account else []
        self.issue('manual_credit_check', 'Snapshot data cannot establish current credit release.',
                   'Check current credit status manually in Orbit. Only Tom can authorise credit decisions.',
                   owner='Tom / credit control', evidence=account_ids)
        self.issue('snapshot_limitations', 'Inputs are snapshots; reference freshness may be unknown.',
                   'Verify current records. Generation time is not source freshness; no stock or delivery promise is implied.')
        params = context.get('export_parameters', {})
        vat = params.get('VATRT')
        if vat:
            try:
                if decimal_number(vat['PVAL']) / 100 != VAT_RATE:
                    self.issue('vat_conflict', f"Export VATRT is {vat['PVAL']}%; proposed calculations use the user-approved 20%.",
                               'Confirm the applicable VAT treatment before entry; do not reuse the export rate blindly.',
                               owner='Accounts', evidence=[vat['source_id']])
            except ValueError:
                self.issue('vat_unknown', 'Export VAT rate cannot be interpreted.',
                           'Confirm VAT; calculations use the user-approved prototype rate.', owner='Accounts', evidence=[vat['source_id']])
        if self.account:
            allowed = {value.strip() for value in params.get('ACSTCDS', {}).get('PVAL', '').split(',') if value.strip()}
            raw = self.account.get('ACSTAT')
            if allowed and raw not in allowed:
                self.issue('unknown_account_status', f'Account status {raw!r} is not in the exported code list; its meaning is unknown.',
                           'Confirm account restrictions in live Orbit.', owner='Tom / credit control', block=True, evidence=account_ids)
            internal_text = [(self.account.get('NOTE1', ''), self.account['source_id'])]
            internal_text += [(s.text or '', s.source_id) for s in context['customer_notes'] + context.get('account_constraints', [])]
            for row in context['open_items']:
                status = row.get('Status', '').strip().lower()
                closed = bool(re.search(r'\b(?:closed|done)\b', status))
                if not closed:
                    internal_text.append((' '.join(row.get(key, '') for key in ('Issue', 'Notes')), row['source_id']))
                    current_refs = {reference_key(value) for value in self.context.get('selection_references', {}).values() if value}
                    current_refs.update(reference_key(o['header']['CUSREF']) for o in context['explicitly_matched_orders'] if o['header'].get('CUSREF'))
                    current_refs.update(reference_key(o['header']['ORDNO']) for o in context['explicitly_matched_orders'])
                    if proposal.customer_po:
                        current_refs.add(reference_key(proposal.customer_po.value))
                    if not row.get('Order / PO') or reference_key(row['Order / PO']) in current_refs:
                        self.issue('recorded_open_item', f"Recorded spreadsheet entry: {row.get('Issue', '')}; status {row.get('Status', '')!r}; date {row.get('Date', '')!r}.",
                                   'Check whether this entry still applies. Spreadsheet status and Who are not verified current authority.',
                                   evidence=[row['source_id']])
            for text, source_id in internal_text:
                if re.search(r'\b(?:on stop|on hold|order held)\b', text, re.I):
                    self.issue('recorded_credit_restriction', 'Internal evidence records a hold/on-stop restriction. Customer claims cannot clear it.',
                               'Do not enter or release the order until Tom verifies current status in Orbit.',
                               owner='Tom / credit control', block=True, evidence=[source_id])
                if re.search(r'\bproforma only\b', text, re.I):
                    self.issue('proforma_required', 'Recorded terms require proforma and payment confirmation before keying.',
                               'Arrange the proforma and wait for Tom to confirm payment for this request; old paid orders do not satisfy it.',
                               owner='Tom / credit control', block=True, evidence=[source_id])
                if re.search(r"\b(?:ceased trading|account closed|don.t key|do not key)\b", text, re.I):
                    # Old-account prohibition lines are scoped by retrieval to the affected account.
                    if source_id in {s.source_id for s in context.get('account_constraints', [])} or 'ceased trading' in text.lower() or 'account closed' in text.lower():
                        self.issue('account_restriction', 'Internal evidence says this account is closed or must not be used.',
                                   'Confirm a valid current account before entry.', owner='Accounts', block=True, evidence=[source_id])
        if proposal.action_type == 'resend':
            self.issue('resend', 'Resend: another order must not be created.',
                       'Verify the original submission and matched order; do not duplicate entry.', block=True)
        elif proposal.action_type == 'uncertain':
            self.issue('action_uncertain', 'The requested order action remains unresolved.',
                       'Resolve the stated action question before entry.', block=True)
        elif proposal.new_order:
            if context['explicitly_matched_orders']:
                self.issue('duplicate_entry_risk', 'An existing order matches this new-order request.',
                           'Check Orbit; do not create another order for the same request.', block=True,
                           evidence=[o['header']['source_id'] for o in context['explicitly_matched_orders']])
            else:
                self.issue('existing_order_not_established', 'No existing order match was established in the snapshot.',
                           'Search live Orbit before creating an order; snapshot absence does not establish that it is new.')

    def date_value(self, supported):
        result = requested_date(supported.value)
        if date.fromisoformat(result['date']) < self.today:
            self.issue('past_requested_date', f"Requested date/window starts on {result['date']}, before review date {self.today}.",
                       'Reconfirm current delivery requirements; do not promise or enter an expired date.',
                       owner='Karen / operations', block=True, evidence=supported.evidence_ids)
        if result['kind'] != 'exact_date' or supported.basis == 'inferred_suggestion':
            self.issue('delivery_date_confirmation', 'The supplied value is a deadline/window or inferred date, not a confirmed delivery commitment.',
                       'Confirm the intended date and delivery feasibility with Karen.', owner='Karen / operations', evidence=supported.evidence_ids)
        return result

    def line(self, proposed):
        item = {'line_id': proposed.line_id, 'requested_description': proposed.description.model_dump(exclude_none=True),
                'field_basis': {field: getattr(proposed, field).basis for field in ('description', 'sku', 'quantity', 'unit')
                                if getattr(proposed, field) is not None},
                'evidence_ids': list(dict.fromkeys(key for field in ('description', 'sku', 'quantity', 'unit')
                                                 if getattr(proposed, field) is not None
                                                 for key in getattr(proposed, field).evidence_ids))}
        if proposed.sku is None or proposed.quantity is None or proposed.unit is None:
            self.issue('line_incomplete', f'Line {proposed.line_id} lacks a supported SKU, quantity or unit.',
                       'Confirm the missing details before entry.', block=True, evidence=proposed.description.evidence_ids)
            return item
        sku = proposed.sku.value
        item['sku'] = sku
        item['sku_basis'] = proposed.sku.basis
        try:
            row = self.catalogue[sku]
            count = quantity(proposed.quantity.value)
            unit = canonical_unit(proposed.unit.value, row['unit'], description=row['description'])
            if count != count.to_integral_value():
                raise ValueError('Fractional quantities of catalogue count units need confirmation')
            price = self.pricing.price(sku, self.request_date)
            amount = money(count * price.amount)
            item.update(quantity=format(count, 'f'), unit=unit, quantity_basis='order_total',
                        description=row['description'], price=price.display(), line_net=money_text(amount))
            if str(row.get('status', '')).lower() != 'current':
                self.issue('product_not_current', f"{sku} is recorded as {row.get('status')}; replacement: {row.get('replaced_by') or 'unknown'}.",
                           'Confirm a supported replacement and its specification/price; do not substitute automatically.',
                           owner='Karen / operations', block=True, evidence=[row['source_id']])
            if proposed.sku.basis == 'inferred_suggestion':
                self.issue('inferred_product', f'{sku} is an inferred product suggestion.',
                           'Double-check the product, specification and unit against the request.', evidence=proposed.sku.evidence_ids)
            if proposed.customer_quoted_price:
                quote = proposed.customer_quoted_price
                if quote.currency.upper() != 'GBP':
                    raise ValueError('Quoted currency is not established as GBP; no exchange conversion is supported')
                expected = price.amount if quote.price_basis == 'per_unit' else amount if quote.price_basis == 'line_total' else None
                if expected is None:
                    raise ValueError('Line price basis is unknown or is not a unit/line amount')
                customer = exclusive_amount(quote.amount.value, quote.vat_basis)
                item['customer_quoted_price_ex_vat'] = money_text(customer)
                if customer != expected:
                    self.issue('price_conflict', f'{sku}: customer amount {money_text(customer)} ex VAT differs from recorded {money_text(expected)} ({quote.price_basis}).',
                               'Karen must review the discrepancy before entry; do not treat the PO price as approval.',
                               owner='Karen / operations', block=True, evidence=quote.amount.evidence_ids + price.evidence_ids)
        except (ValueError, KeyError) as exc:
            self.issue('line_pricing_unresolved', f'Line {proposed.line_id}: {exc}',
                       'Confirm product, unit and pricing terms with Karen; do not guess.', owner='Karen / operations', block=True,
                       evidence=proposed.sku.evidence_ids + proposed.quantity.evidence_ids + proposed.unit.evidence_ids)
        return item

    def new_order(self):
        proposed = self.proposal.new_order
        header = {}
        for name in ('delivery_destination', 'delivery_instructions'):
            value = getattr(proposed.header, name)
            if value:
                header[name] = value.model_dump(exclude_none=True)
        if proposed.header.requested_delivery_date:
            value = proposed.header.requested_delivery_date
            try:
                header['requested_delivery_date'] = {**self.date_value(value), 'basis': value.basis, 'evidence_ids': value.evidence_ids}
            except ValueError as exc:
                header['requested_delivery_date'] = value.model_dump(exclude_none=True)
                self.issue('date_unresolved', str(exc), 'Confirm the requested delivery date/window.',
                           owner='Karen / operations', block=True, evidence=value.evidence_ids)
        if not header.get('delivery_destination') and self.account:
            address = ', '.join(self.account.get(key, '') for key in ('ADR1', 'ADR2', 'PCODE') if self.account.get(key))
            header['recorded_account_address'] = {'value': address, 'basis': 'recorded_internal',
                                                   'evidence_ids': [self.account['source_id']]}
            notes = ' '.join(s.text or '' for s in self.context['customer_notes']) + ' ' + self.account.get('NOTE1', '')
            multiple = bool(re.search(r'(?i)(?:check which site|po says which hotel|sites - check|check del addr)', notes))
            self.issue('delivery_destination_confirmation', 'A delivery destination was not established from this request; the account address is supporting information only.',
                       'Confirm the delivery site and full address.', block=multiple, evidence=[self.account['source_id']])
        self.lines = [self.line(line) for line in proposed.lines]
        if not self.lines:
            self.issue('no_order_lines', 'No supported order lines were extracted.', 'Confirm the complete requested order.', block=True)
        self.changes = [{'operation': 'new_order', 'header': header, 'lines': self.lines}]
        for discount in proposed.customer_discounts:
            self.issue('customer_discount_unapproved', f'Customer-requested discount: {discount.description.value}. It has not been applied to calculated prices.',
                       'Karen must confirm any exception against recorded terms; do not layer it onto net band/contract prices.',
                       owner='Karen / operations', block=True, evidence=discount.description.evidence_ids)
        if self.lines and all('line_net' in line for line in self.lines):
            net = sum((Decimal(line['line_net']) for line in self.lines), Decimal('0'))
            vat = money(net * VAT_RATE)
            self.totals = {'net': money_text(net), 'vat_rate': str(VAT_RATE), 'vat': money_text(vat),
                           'gross': money_text(net + vat), 'basis': 'Unit prices rounded first; VAT rounded on net total; no unapproved extra discounts.'}
            for quoted in proposed.customer_quoted_totals:
                try:
                    if quoted.currency.upper() != 'GBP' or quoted.price_basis != 'order_total':
                        raise ValueError('Quoted total currency/price basis is not established')
                    label = (quoted.label or '').lower().strip()
                    if label in {'vat', 'vat 20%', 'tax'}:
                        expected = vat
                        customer = money(quoted.amount.value)
                    elif quoted.vat_basis == 'inclusive':
                        expected = net + vat
                        customer = money(quoted.amount.value)
                    elif quoted.vat_basis == 'exclusive' or label in {'net', 'subtotal', 'net total'}:
                        expected = net
                        customer = money(quoted.amount.value)
                    else:
                        raise ValueError('Quoted total VAT basis is unknown')
                    if customer != expected:
                        self.issue('quoted_total_conflict', f"Customer total {quoted.amount.value} ({quoted.label or quoted.vat_basis}) differs from the code-calculated amount.",
                                   'Karen must review prices, quantities, VAT and any requested discounts.',
                                   owner='Karen / operations', block=True, evidence=quoted.amount.evidence_ids)
                except ValueError as exc:
                    self.issue('quoted_total_unknown', str(exc), 'Confirm what the quoted total includes.',
                               owner='Karen / operations', block=True, evidence=quoted.amount.evidence_ids)
        elif self.lines:
            known = sum((Decimal(line['line_net']) for line in self.lines if 'line_net' in line), Decimal('0'))
            self.totals = {'known_lines_net_subtotal': money_text(known), 'complete': False,
                           'reason': 'At least one line cannot be calculated; no complete order total is asserted.'}

    def target(self, change):
        orders = self.context['explicitly_matched_orders']
        requested = change.target.orbit_order_number if change.target else None
        matches = [order for order in orders if requested is None or order['header']['ORDNO'] == requested]
        if len(matches) != 1:
            raise ValueError('No unique explicitly matched amendment order')
        order = matches[0]
        target = change.target
        line = None
        if target and (target.orbit_line_number is not None or target.sku is not None):
            matches = [row for row in order['lines'] if
                       (target.orbit_line_number is None or row['LNNO'] == target.orbit_line_number) and
                       (target.sku is None or row['STKCD'] == target.sku.value)]
            if len(matches) != 1:
                raise ValueError('No unique matched order line')
            line = matches[0]
        return order, line

    def amendments(self):
        for change in self.proposal.amendment.changes:
            try:
                order, line = self.target(change)
                record = {'change_id': change.change_id, 'operation': change.operation, 'field': change.field,
                          'orbit_order_number': order['header']['ORDNO'], 'evidence_ids': change.evidence_ids,
                          'request_status': 'Unverified customer request — confirm before applying'}
                if change.operation == 'replace' and change.field == 'delivery_date':
                    old = order_date(order['header']['DELDT']).isoformat() if order['header'].get('DELDT') else None
                    new = self.date_value(change.proposed_value)
                    if new['kind'] == 'exact_date' and old == new['date']:
                        continue
                    record.update(old_value=old, old_raw_value=order['header'].get('DELDT'), proposed_value=new,
                                  recorded_evidence_ids=[order['header']['source_id']])
                elif change.operation == 'replace' and change.field in {'delivery_destination', 'delivery_instructions'}:
                    record.update(old_value=None, old_value_status='Not available in the order export',
                                  proposed_value=change.proposed_value.model_dump(exclude_none=True))
                    self.issue('unverified_destination_change', 'The request changes delivery details, but the existing order destination/instructions are not exported.',
                               'Verify the current order details in live Orbit and confirm the requested change with Karen.',
                               owner='Karen / operations', block=True, evidence=change.evidence_ids)
                elif change.operation == 'replace' and change.field == 'line_quantity':
                    if line is None:
                        raise ValueError('Quantity change has no established line target')
                    sku = line['STKCD']
                    row = self.catalogue.get(sku)
                    if row is None or change.unit is None:
                        raise ValueError('Catalogue unit or requested quantity unit is unresolved')
                    unit = canonical_unit(change.unit.value, row['unit'], description=row['description'])
                    old = quantity(line['QTY'])
                    requested = quantity(change.proposed_value.value)
                    new = old + requested if change.quantity_basis == 'additional' else requested
                    if new != new.to_integral_value():
                        raise ValueError('Fractional count-unit quantity requires confirmation')
                    dispatched = quantity(line['QTYDS'], allow_zero=True)
                    if new < dispatched:
                        self.issue('below_dispatched_quantity', 'Proposed replacement total is below quantity already dispatched.',
                                   'Karen must resolve the dispatched/return position before amendment.',
                                   owner='Karen / operations', block=True, evidence=[line['source_id']] + change.evidence_ids)
                    if old == new:
                        continue
                    net = orbit_net_price(line)
                    record.update(orbit_line_number=line['LNNO'], sku=sku, unit=unit,
                                  old_value=format(old, 'f'), proposed_value=format(new, 'f'),
                                  quantity_basis='replacement_total', requested_quantity_basis=change.quantity_basis,
                                  requested_value=change.proposed_value.value,
                                  recorded_net_unit_price_ex_vat=money_text(net),
                                  proposed_line_net=money_text(net * new),
                                  price_instruction='Recorded UPRC in pence less DISC once; do not reapply the account band.',
                                  recorded_evidence_ids=[line['source_id']])
                    expected = self.pricing.price(sku, self.request_date)
                    if net != expected.amount:
                        self.issue('recorded_price_conflict', 'Recorded order net price differs from the supplied current band/contract price; it has not been overwritten.',
                                   'Karen must check the price basis; this quantity change does not authorise repricing.',
                                   owner='Karen / operations', block=True, evidence=[line['source_id']] + expected.evidence_ids)
                elif change.operation == 'add_line':
                    item = self.line(change.added_line)
                    record.update(old_value=None, proposed_value=item)
                    if any(quantity(row['QTYDS'], allow_zero=True) > 0 for row in order['lines']):
                        self.issue('addition_after_dispatch', 'The matched order has dispatched quantities.',
                                   'Karen must confirm how to handle the additional line.', owner='Karen / operations', block=True,
                                   evidence=[order['header']['source_id']])
                elif change.operation == 'remove_line' and line is not None:
                    record.update(orbit_line_number=line['LNNO'], sku=line['STKCD'], old_value={'quantity': line['QTY']}, unit=self.catalogue.get(line['STKCD'], {}).get('unit', 'Unknown — confirm'), proposed_value=None,
                                  recorded_evidence_ids=[line['source_id']])
                    if quantity(line['QTYDS'], allow_zero=True) > 0:
                        self.issue('removal_after_dispatch', 'The requested line removal affects dispatched goods.',
                                   'Karen must confirm the return/correction process.', owner='Karen / operations', block=True,
                                   evidence=[line['source_id']])
                else:
                    self.issue('unsupported_amendment', f'{change.operation}/{change.field} requires an undocumented workflow or pricing/status decision.',
                               'Karen must review the requested action; do not invent Orbit fields or status-code meanings.',
                               owner='Karen / operations', block=True, evidence=change.evidence_ids)
                    record.update(proposed_value=change.proposed_value.model_dump(exclude_none=True) if change.proposed_value else 'Requested operation requires manual review',
                                  applicable=False)
                self.changes.append(record)
            except (ValueError, KeyError) as exc:
                self.issue('amendment_unresolved', f'{change.change_id}: {exc}',
                           'Verify the original order and requested replacement values before entry.',
                           owner='Karen / operations', block=True, evidence=change.evidence_ids)
                self.changes.append({'change_id': change.change_id, 'field': change.field,
                                     'raw_request': change.model_dump(exclude_none=True), 'applicable': False})

    def current_status(self):
        result = []
        stock = {row['STKCD']: row for row in self.context['stock_records']}
        for order in self.context['explicitly_matched_orders']:
            header = order['header']
            source = self.context['sources'][header['source_id']]
            result.append({'orbit_order_number': header['ORDNO'], 'status_code_raw': header.get('STAT'),
                           'status_meaning': 'Undocumented; verify in Orbit',
                           'snapshot_timestamp_raw': source.timestamp, 'snapshot_timezone': 'Unknown',
                           'evidence_ids': [header['source_id']],
                           'lines': [{'sku': row['STKCD'], 'ordered_quantity_raw': row['QTY'],
                                      'dispatched_quantity_raw': row['QTYDS'],
                                      'recorded_unit_raw': stock.get(row['STKCD'], {}).get('UOM'),
                                      'evidence_ids': [row['source_id']]} for row in order['lines']]})
        return result or [{'message': 'No existing order match established; verify live Orbit before entry.'}]

    def run(self):
        self.baseline()
        if self.proposal.new_order:
            self.new_order()
        elif self.proposal.amendment:
            self.amendments()
        for issue in self.proposal.issues:
            self.issue('model_reported_issue', issue.description, issue.next_action,
                       owner=issue.owner or 'Order processor', evidence=issue.evidence_ids, origin='llm_proposal')
        blocked = any(issue['blocks_entry'] for issue in self.issues)
        return {'status': 'validated', 'readiness': 'Do not proceed' if blocked else 'Checks required',
                'entry_section_label': 'Proposed changes — do not apply yet' if blocked else 'Changes to apply',
                'current_order_status': self.current_status(), 'changes_to_apply': self.changes,
                'calculations': self.totals, 'issues': self.issues,
                'uncertainties': [item.model_dump() for item in self.proposal.uncertainties],
                'vat_policy': {'rate': str(VAT_RATE), 'origin': 'User-approved prototype rule'},
                'requires_human_review': True}


def apply_business_rules(record, *, today=None):
    """Return a new review artefact, preserving the exact preparation as evidence."""
    output = {'schema_version': '1.0', 'report_id': uuid4().hex,
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'preparation': copy.deepcopy(record), 'business_rules_applied': False}
    try:
        if record.get('status') != 'prepared':
            raise ValueError('Preparation failed; no successful proposal is available to validate. ' + str(record.get('error', '')))
        proposal = PreparationOutput.model_validate(record['proposal'])
        context = copy.deepcopy(record['context'])
        if proposal.email_id != record['trigger']['email_id'] or context['email_id'] != proposal.email_id:
            raise ValueError('Preparation/context/trigger email IDs disagree')
        context['sources'] = {key: Source(**source) if isinstance(source, dict) else source
                              for key, source in context['sources'].items()}
        if any(key != source.source_id for key, source in context['sources'].items()):
            raise ValueError('Source catalogue keys and evidence identifiers disagree')
        for field in ('customer_notes', 'account_constraints'):
            context[field] = [Source(**source) if isinstance(source, dict) else source
                              for source in context.get(field, [])]
        context['selection_references'] = record['classification'].get('references', {})
        for alert in record.get('report_alerts', []):
            if alert not in context['report_alerts']:
                context['report_alerts'].append(alert)
        validate_preparation(proposal, context, record['classification'])
        output['validation'] = _Review(context, proposal, today or datetime.now(timezone.utc).date()).run()
        output['business_rules_applied'] = True
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        output['validation'] = {'status': 'failed', 'readiness': 'Do not proceed',
                                'entry_section_label': 'Proposed changes — do not apply yet',
                                'error': str(exc), 'changes_to_apply': [], 'requires_human_review': True}
    return output


def validate_saved_preparations(input_path, output_dir):
    """Review one preparation JSON or its run manifest; never call OpenAI."""
    input_path = Path(input_path)
    document = json.loads(input_path.read_text(encoding='utf-8'))
    if isinstance(document, dict) and 'results' in document and 'selected_emails' in document:
        entries = document['results']
        if not isinstance(entries, list) or len(entries) != document['selected_emails']:
            raise ValueError('Preparation manifest does not account for every selected email')
        jobs = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError('Malformed preparation manifest entry')
            if 'path' in item:
                path = input_path.parent / Path(item['path']).name
                try:
                    record = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    record = {'status': 'failed', 'error': f'Cannot load preparation: {exc}',
                              'trigger': {'email_id': item.get('email_id', 'Unknown email')}}
                jobs.append((path, record))
            else:
                record = item.get('unsaved_result', {'status': 'failed', 'error': item.get('error', 'Preparation file missing')})
                jobs.append((input_path, record))
    else:
        jobs = [(input_path, document)]
    results = []
    for path, record in jobs:
        reviewed = apply_business_rules(record)
        saved = save_json_artifact(reviewed, output_dir, prefix='validated_preparation')
        results.append({'source': str(path), 'path': str(saved.resolve()),
                        'email_id': reviewed['preparation'].get('trigger', {}).get('email_id'),
                        'report_id': reviewed['report_id'],
                        'status': reviewed['validation']['status'], 'readiness': reviewed['validation']['readiness']})
    summary = {'results': results, 'validated': sum(row['status'] == 'validated' for row in results),
               'failed': sum(row['status'] == 'failed' for row in results)}
    manifest = save_json_artifact(summary, output_dir, prefix='validation_run')
    return {**summary, 'manifest_path': str(manifest.resolve())}
