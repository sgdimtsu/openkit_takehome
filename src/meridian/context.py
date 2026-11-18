"""Deterministic, isolated context retrieval for one classified email."""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .sources import Source

HISTORY_LIMIT = 3  # Fixed requirement for this prototype.


def name_key(value):
    text = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode()
    words = re.findall(r'[a-z0-9]+', re.sub(r"['’]", '', text.lower()))
    return ' '.join(word for word in words if word not in {'the', 'ltd', 'limited'})


def reference_key(value):
    return ''.join(re.findall(r'[a-z0-9]+', str(value).lower()))


def order_date(value):
    """Normalise UK, ISO, compact ISO and approved Excel-1900 order dates."""
    text = str(value).strip()
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y%m%d', '%Y-%m-%d'):
        if fmt == '%Y%m%d' and len(text) != 8:
            continue
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    if re.fullmatch(r'\d{1,5}(?:\.0+)?', text):
        return (datetime(1899, 12, 30) + timedelta(days=int(float(text)))).date()
    raise ValueError(f'Unrecognised order date: {value!r}')


@dataclass
class CustomerMatch:
    status: str
    account_id: Optional[str]
    candidate_account_ids: list[str]
    reason: str
    evidence_ids: list[str]


class ContextRetriever:
    """Build indexes once; no writes, model calls or cross-email request merging."""

    def __init__(self, internal, inbox):
        self.data = internal
        self.emails = {email['id']: email for email in inbox.emails}
        self.sources = {**internal.sources, **inbox.sources}
        self.aliases = {}
        self.contacts = {}
        self.sites = {}
        self.notes_by_customer = {}
        for account, row in internal.customers_by_id.items():
            self._alias(re.sub(r'\([^)]*\)', '', row['CUSNM']), account, row['source_id'])
        notes = internal.sources[internal.documents['account_notes']]
        self.general_notes = Source(notes.source_id + ':preamble', notes.relative_path,
                                    notes.display_name + ' — introductory notes',
                                    'Before first customer heading', notes.timestamp,
                                    notes.text.split('\n### ', 1)[0])
        self.sources[self.general_notes.source_id] = self.general_notes
        for number, section in enumerate(re.split(r'(?m)^### ', notes.text)[1:], 1):
            heading, _, body = section.partition('\n')
            source_id = notes.source_id + f':section:{number}'
            source = Source(source_id, notes.relative_path, heading, f'Section: {heading}',
                            notes.timestamp, heading + '\n' + body)
            self.sources[source_id] = source
            aliases = [re.sub(r'\([^)]*\)', '', part).strip() for part in heading.split('/')]
            # Parse explicit account assignments only, never incidental old-account mentions.
            positive = re.findall(r'(?im)^-\s*(?:\*\*)?(?:Orbit(?: account)?\s*:\s*|Use\s+)([A-Za-z0-9]+)', body)
            accounts = {account for account in positive if account in internal.customers_by_id}
            preferred = bool(re.search(r'(?im)^-\s*(?:\*\*)?Use\s+', body))
            if not positive:
                for alias in aliases:
                    accounts.update(self.aliases.get(name_key(alias), {}))
            if len(accounts) == 1:
                account = next(iter(accounts))
                # Account notes identify contacts in bullet leads or after "Orders come from".
                for contact in re.findall(
                        r"(?m)(?:^- |(?<=\. ))(?:Orders come from )?"
                        r"([A-Z][a-z]+ [A-Z][a-z]+(?:['’][A-Z][a-z]+)?)(?=,| \(| orders\b)",
                        body):
                    self.contacts.setdefault(name_key(contact), {}).setdefault(account, set()).add(source_id)
                for sites in re.findall(r'(?im)^- Sites:\s*(.+)$', body):
                    for site in re.sub(r'\([^)]*\)', '', sites).split(','):
                        if name_key(site):
                            self.sites.setdefault(account, {}).setdefault(name_key(site), set()).add(source_id)
            for account in accounts:
                self.notes_by_customer.setdefault(account, []).append(source)
            for alias in aliases:
                if preferred and len(accounts) == 1:
                    self.aliases[name_key(alias)] = {}
                for account in accounts:
                    self._alias(alias, account, source_id)
        self.account_constraints = {}
        for line_number, line in enumerate(notes.text.splitlines(), 1):
            if not re.search(r"(?i)(?:don.t key|do not key|account closed|ceased trading)", line):
                continue
            for account in internal.customers_by_id:
                if not re.search(r'(?<![A-Za-z0-9])' + re.escape(account) + r'(?![A-Za-z0-9])', line):
                    continue
                source_id = notes.source_id + f':account-restriction:{account}:line:{line_number}'
                source = Source(source_id, notes.relative_path, f'Account restriction — {account}',
                                f'Line {line_number}', notes.timestamp, line)
                self.sources[source_id] = source
                self.account_constraints.setdefault(account, []).append(source)
        self.orders_by_reference = {}
        for order in internal.orders_by_id.values():
            if order.get('CUSREF'):
                self.orders_by_reference.setdefault(reference_key(order['CUSREF']), []).append(order)
        self.history = {}
        self.history_problems = {}
        for account, orders in internal.orders_by_customer.items():
            dated, problems = [], []
            for order in orders:
                try:
                    dated.append((order_date(order.get('ORDDT')), order['ORDNO'], order))
                except ValueError:
                    problems.append(order['ORDNO'])
            dated.sort(key=lambda item: (item[0], item[1]), reverse=True)
            self.history[account] = [self._order(item[2], item[0]) for item in dated[:HISTORY_LIMIT]]
            self.history_problems[account] = problems
        self.items_by_customer, self.unassigned_items = {}, []
        for row in internal.open_items:
            candidates = self._name_candidates(row.get('Customer', ''), allow_short=True)
            if len(candidates) == 1:
                self.items_by_customer.setdefault(next(iter(candidates)), []).append(row)
            else:
                self.unassigned_items.append(row)

    def _alias(self, value, account, evidence):
        key = name_key(value)
        if key:
            self.aliases.setdefault(key, {}).setdefault(account, set()).add(evidence)

    def _name_candidates(self, value, allow_short=False):
        key = name_key(value)
        if not key:
            return set()
        if key in self.aliases:
            return set(self.aliases[key])
        if allow_short or len(key.split()) >= 2:
            return {account for alias, accounts in self.aliases.items()
                    if alias.startswith(key + ' ') or key.startswith(alias + ' ')
                    for account in accounts}
        return set()

    def _order(self, order, normalised=None):
        if normalised is None:
            try:
                normalised = order_date(order.get('ORDDT'))
            except ValueError:
                pass
        return {'header': order, 'lines': self.data.lines_by_order.get(order['ORDNO'], []),
                'order_date_normalised': normalised.isoformat() if normalised else None}

    @staticmethod
    def _stated(value, text):
        # Split letter/number runs so hyphens/spaces and numeric commas can vary.
        def tokens(value):
            value = re.sub(r'(?<=\d),(?=\d)', '', str(value).lower())
            return re.findall(r'[a-z]+|\d+', value)
        wanted, actual = tokens(value), tokens(text)
        return bool(wanted) and any(actual[i:i + len(wanted)] == wanted
                                   for i in range(len(actual) - len(wanted) + 1))

    def _references(self, email, references):
        found, issues, unmatched_pos = {}, [], []
        text = email['subject'] + '\n' + email['body']
        for kind, value in references.items():
            if value is None:
                continue
            if kind not in {'customer_po', 'orbit_order_number'}:
                raise ValueError(f'Unsupported reference type: {kind}')
            if not self._stated(value, text):
                issues.append(f'Candidate {kind} {value!r} is not stated in this email; not used.')
                continue
            if kind == 'customer_po':
                orders = self.orders_by_reference.get(reference_key(value), [])
            else:
                digits = str(value).replace(',', '').replace(' ', '')
                order = self.data.orders_by_id.get(int(digits)) if digits.isdigit() else None
                orders = [order] if order else []
            if not orders:
                message = f'No existing order match established for {kind} {value!r}; absence is not proof of a new order.'
                (unmatched_pos if kind == 'customer_po' else issues).append(message)
            for order in orders:
                found[order['ORDNO']] = order
        return list(found.values()), issues, unmatched_pos

    def match_customer(self, email, customer, referenced_orders):
        name, stated_id = customer.get('name_as_stated'), customer.get('account_id_as_stated')
        candidates = self._name_candidates(name) if name else set()
        evidence = set()
        unresolved = bool(name and not candidates)
        if stated_id:
            if (stated_id in self.data.customers_by_id and
                    self._stated(stated_id, email['subject'] + '\n' + email['body'])):
                if candidates and stated_id not in candidates:
                    unresolved = True
                candidates.add(stated_id)
            else:
                unresolved = True
        owners = {order['CUSNO'] for order in referenced_orders}
        if owners:
            if candidates and not owners.issubset(candidates):
                unresolved = True
            candidates.update(owners)
            evidence.update(order['source_id'] for order in referenced_orders)
        if not name and not stated_id and not owners:
            # Fax gateway sender is not the customer; use an explicit phone number.
            text = email['subject'] + '\n' + email['body']
            numbers = {re.sub(r'\D', '', number) for number in
                       re.findall(r'(?<!\d)0\d[\d -]{6,}\d(?!\d)', text)}
            candidates = {account for account, row in self.data.customers_by_id.items()
                          if re.sub(r'\D', '', row.get('TELNO', '')) in numbers}
        for ids in self.aliases.get(name_key(name or ''), {}).values():
            evidence.update(ids)
        sender = name_key(email['from'].split('<', 1)[0].strip(' "'))
        contact_matches = self.contacts.get(sender, {})
        corroborated = False
        if len(contact_matches) == 1:
            contact_account, note_ids = next(iter(contact_matches.items()))
            if candidates and contact_account not in candidates:
                candidates.add(contact_account)
                evidence.update(note_ids)
                unresolved = True
            elif (not name or name_key(name) == sender) and not stated_id and not owners:
                candidates.add(contact_account)
                evidence.update(note_ids)
                unresolved = False
                corroborated = True
        if len(candidates) == 1 and name and not unresolved and not corroborated:
            account = next(iter(candidates))
            text = email['subject'] + '\n' + email['body']
            named = name_key(name)
            for site, note_ids in self.sites.get(account, {}).items():
                if named.endswith(' ' + site) and self._stated(site, text):
                    base = named[:-(len(site) + 1)]
                    if set(self.aliases.get(base, {})) == {account}:
                        evidence.update(note_ids)
                        corroborated = True
                        break
        for account in candidates:
            evidence.add(self.data.customers_by_id[account]['source_id'])
        # Prefix-only name matches are candidates unless independent evidence corroborates.
        if name and name_key(name) not in self.aliases and not owners and not corroborated:
            unresolved = True
        if len(candidates) == 1 and not unresolved:
            account = next(iter(candidates))
            if account in self.data.customers_by_id:
                return CustomerMatch('matched', account, [account],
                                     'Unique account supported by supplied names, account notes or explicit order reference.',
                                     sorted(evidence))
        return CustomerMatch('unresolved', None, sorted(candidates),
                             'Confirm the customer account: supplied identifiers are missing, ambiguous or conflicting.',
                             sorted(evidence))

    def prepare(self, selection, attachment_sources=(), *, attachment_results=()):
        email_id = selection['email_id']
        if email_id not in self.emails:
            raise ValueError(f'Unknown trigger email: {email_id}')
        email = self.emails[email_id]
        references = selection.get('references', {})
        referenced, reference_issues, unmatched_pos = self._references(email, references)
        match = self.match_customer(email, selection.get('customer', {}), referenced)
        alerts = []
        def alert(code, title, message, blocks=True):
            alerts.append({'code': code, 'title': title, 'message': message,
                           'blocks_entry': blocks, 'placement': 'report_header_and_things_to_confirm'})
        account = match.account_id
        if match.status != 'matched':
            alert('customer_match_unresolved', 'Customer match unresolved — do not proceed',
                  'Confirm the correct account before entry. No customer history assigned. Candidates: ' +
                  (', '.join(match.candidate_account_ids) or 'none established'))
        for message in reference_issues:
            alert('order_reference_unresolved', 'Order reference requires checking', message)
        for message in unmatched_pos:
            alert('order_reference_unresolved', 'Order reference requires checking', message,
                  blocks=selection.get('action_type') != 'new_order_request')
        matched_orders = [self._order(order) for order in referenced if account and order['CUSNO'] == account]
        if len(matched_orders) > 1:
            alert('multiple_order_matches', 'Multiple existing orders matched',
                  'Confirm which order the request concerns before applying changes.')
        history = self.history.get(account, []) if account else []
        if account and len(history) < HISTORY_LIMIT:
            alert('insufficient_history', 'Insufficient historical support',
                  f'Only {len(history)} dated orders available; history limit not expanded.', False)
        if account and self.history_problems.get(account):
            alert('invalid_history_dates', 'Latest-order history is uncertain',
                  'Unrecognised dates prevent reliable last-three selection. Check orders: ' +
                  ', '.join(map(str, self.history_problems[account])))
        notes = self.notes_by_customer.get(account, []) if account else []
        items = self.items_by_customer.get(account, []) if account else []
        verified_refs = {reference_key(order['CUSREF']) for order in referenced if order.get('CUSREF')}
        verified_refs.update(reference_key(order['ORDNO']) for order in referenced)
        def possibly_same_customer(row):
            if not account:
                return False
            words = name_key(row.get('Customer', '')).split()
            # Truncated spreadsheet names are suggestions only, never assignments.
            return len(words) >= 2 and any(
                account in accounts and len(words) <= len(alias.split()) and
                all(target.startswith(word) for word, target in zip(words, alias.split()))
                for alias, accounts in self.aliases.items())
        uncertain_items = [row for row in self.unassigned_items
                           if reference_key(row.get('Order / PO', '')) in verified_refs
                           or possibly_same_customer(row)]
        if uncertain_items:
            alert('spreadsheet_customer_unresolved', 'Spreadsheet customer match unresolved',
                  'Potentially relevant spreadsheet entries have uncertain customer names; check them before use.')
        attachment_paths = {self.sources[item['source_id']].relative_path for item in email['attachments']}
        attachment_sources = list(attachment_sources)
        attachment_results = list(attachment_results)
        processed_paths = set()
        for result in attachment_results:
            if result.relative_path not in attachment_paths:
                raise ValueError(f'Attachment result outside trigger email: {result.relative_path}')
            processed_paths.add(result.relative_path)
            attachment_sources.extend(result.sources)
            for error in result.errors:
                alert('attachment_extraction_failed', 'Attachment could not be fully read',
                      result.relative_path + ': ' + error)
        attachment_evidence = []
        for source in attachment_sources:
            if source.relative_path not in attachment_paths:
                raise ValueError(f'Attachment evidence outside trigger email: {source.source_id}')
            if source.source_id in self.sources and self.sources[source.source_id] != source:
                raise ValueError(f'Conflicting attachment evidence: {source.source_id}')
            if any(item.source_id == source.source_id and item != source for item in attachment_evidence):
                raise ValueError(f'Conflicting attachment evidence: {source.source_id}')
            attachment_evidence.append(source)
            if source.extraction_error:
                alert('attachment_page_failed', 'Attachment page could not be fully read',
                      source.display_name + ': ' + source.extraction_error)
            elif source.requires_review:
                alert('ocr_review_required', 'Scanned page requires manual verification',
                      source.display_name + ': OCR can omit or misread content. Check every line, '
                      'quantity, product, price and date against the original page; confidence is not approval.')
        extracted_paths = {source.relative_path for source in attachment_evidence if source.text and not source.extraction_error}
        for attachment in email['attachments']:
            if not attachment['available']:
                alert('missing_attachment', 'Attachment unavailable', attachment['path'])
            elif self.sources[attachment['source_id']].relative_path not in extracted_paths | processed_paths:
                alert('attachment_extraction_pending', 'Attachment content not yet extracted',
                      attachment['filename'] + ': PDF extraction is a separate component.')
        request_text = ' '.join([email['subject'], email['body']] + [s.text or '' for s in attachment_evidence])
        skus = {line['STKCD'] for order in history + matched_orders for line in order['lines']}
        words = set(name_key(request_text).split())
        # Include catalogue records explicitly named by this customer's contract notes.
        note_text = ' '.join(source.text or '' for source in notes)
        skus.update(sku for sku in self.data.prices_by_sku if re.search(
            r'(?<![A-Za-z0-9-])' + re.escape(sku) + r'(?![A-Za-z0-9-])', note_text))
        # Retrieve candidates only, never approve a product match at this stage.
        def compound_description_match(row):
            terms = name_key(row.get('description', '')).split()
            # Join adjacent source words, rather than maintaining product alias answers.
            return bool(words.intersection(a + b for a, b in zip(terms, terms[1:])))
        catalogue = [row for sku, row in self.data.prices_by_sku.items()
                     if sku in skus or sku.casefold() in request_text.casefold()
                     or len(words.intersection(name_key(row.get('description', '')).split())) >= 2
                     or compound_description_match(row)]
        # Follow explicitly recorded catalogue replacements; do not invent SKUs.
        candidates = {row['sku']: row for row in catalogue}
        for row in catalogue:
            replacement = row.get('replaced_by')
            if replacement in self.data.prices_by_sku:
                candidates[replacement] = self.data.prices_by_sku[replacement]
        catalogue = list(candidates.values())
        stock = [self.data.stock_by_sku[sku] for sku in
                 sorted(skus | {row['sku'] for row in catalogue})
                 if sku in self.data.stock_by_sku]
        constraints = self.account_constraints.get(account, []) if account else []
        parameters = {key: self.data.parameters[key] for key in ('VATRT', 'LASTEXP', 'ACSTCDS', 'STATCDS')
                      if key in self.data.parameters}
        used = {email['source_id'], self.general_notes.source_id, *match.evidence_ids}
        used.update(source.source_id for source in constraints)
        used.update(row['source_id'] for row in parameters.values())
        if account:
            used.add(self.data.customers_by_id[account]['source_id'])
        for order in history + matched_orders:
            used.add(order['header']['source_id'])
            used.update(line['source_id'] for line in order['lines'])
        used.update(source.source_id for source in notes)
        for row in items + uncertain_items + catalogue + stock + email['attachments']:
            used.add(row['source_id'])
        evidence = {key: self.sources[key] for key in sorted(used)}
        evidence.update({source.source_id: source for source in attachment_evidence})
        return {'email_id': email_id, 'action_type': selection.get('action_type'),
                'customer_match': match,
                'customer_record': self.data.customers_by_id.get(account),
                'current_request': email, 'explicitly_matched_orders': matched_orders,
                'supporting_history': history, 'history_limit': HISTORY_LIMIT,
                'customer_notes': notes, 'general_account_notes': self.general_notes,
                'account_constraints': constraints, 'export_parameters': parameters,
                'open_items': items, 'unresolved_open_items': uncertain_items,
                'catalogue_candidates': catalogue, 'stock_records': stock, 'attachments': email['attachments'],
                'attachment_extractions': attachment_results,
                'report_alerts': alerts, 'sources': evidence,
                'source_limitations': self.data.warnings}
