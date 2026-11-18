"""Catalogue band prices and two supported, evidence-derived contract forms."""
import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .policy import load_policy
from functools import partial

from .normalise import canonical_unit, decimal_number, money, money_text


@dataclass
class PriceQuote:
    amount: Decimal
    unit: str
    basis: str
    evidence_ids: list[str]

    def display(self, *, policy=None):
        policy = policy if policy is not None else load_policy()
        return {'unit_price_ex_vat': money_text(self.amount, policy=policy), 'currency': policy.pricing.currency,
                'unit': self.unit, 'basis': self.basis, 'discount_already_included': True,
                'instruction': 'This is a net unit price. Do not apply the band/contract discount again.',
                'evidence_ids': self.evidence_ids}


def _month(text):
    names = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
    names.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
    names.update({name[:4].lower(): index for index, name in enumerate(calendar.month_name) if name})
    if text.lower() not in names:
        raise ValueError('Unsupported contract month')
    return names[text.lower()]


class PricingRules:
    def __init__(self, context, *, policy=None):
        self.policy = policy if policy is not None else load_policy()
        self.money = partial(money, policy=self.policy)
        self.account = context['customer_record']
        self.catalogue = {row['sku']: row for row in context['catalogue_candidates']}
        self.contracts = []
        self.errors = []
        for source in context['customer_notes']:
            if self.account:
                declared = {value.upper() for value in re.findall(r'(?i)\bband\s+([A-Za-z]+)\b', source.text or '')}
                # Only explicit band labels supported by CSV field names are compared.
                declared = {value for value in declared if any(
                    f'band_{value.lower()}_gbp' in row for row in self.catalogue.values())}
                actual = self.account['PRCBND'].upper()
                if declared and declared != {actual}:
                    self.errors.append(('Account-note band conflicts with the recorded Orbit band', source.source_id))
            for raw in (source.text or '').splitlines():
                line = raw.replace('*', '')
                if not re.search(r'\bcontract\b', line, re.I):
                    continue
                try:
                    if not re.match(r'(?i)^\s*-?\s*contract\s*[:(]', line):
                        raise ValueError('Contract statement is not an explicit supported contract declaration')
                    contract = {'source_id': source.source_id, 'start': None, 'end': None}
                    period = re.search(r'(?i)contract\s*\(([^)]+)\)', line)
                    if period:
                        match = re.fullmatch(r'([A-Za-z]+)\s+(\d{4})\s+to\s+([A-Za-z]+)\s+(\d{4})', period[1].strip())
                        if not match:
                            raise ValueError('Unsupported contract period')
                        start_month, end_month = _month(match[1]), _month(match[3])
                        contract['start'] = date(int(match[2]), start_month, 1)
                        year = int(match[4])
                        contract['end'] = date(year, end_month, calendar.monthrange(year, end_month)[1])
                        if contract['start'] > contract['end']:
                            raise ValueError('Contract period is reversed')
                    fixed = re.search(r'(?i)fixed at\s*£([\d,.]+)\s+(?:a|per)\s+(\w+)', line)
                    percent = re.search(r'(?i)\bat\s+([\d.]+)%\s+off\s+list', line)
                    if fixed:
                        if line[fixed.end():].strip(' .'):
                            raise ValueError('Additional fixed-price contract conditions require review')
                        skus = re.findall(r'\(([A-Z0-9]+(?:-[A-Z0-9]+)+)\)', line)
                        if len(skus) != 1:
                            raise ValueError('Fixed contract must identify one explicit SKU')
                        contract.update(kind='fixed', sku=skus[0], amount=decimal_number(fixed[1]), unit=fixed[2])
                    elif percent:
                        tail = line[percent.end():].strip()
                        if not re.fullmatch(r'(?i)(?:instead of (?:the )?band)?\.?(?:\s*Everything else at band [A-Za-z]+\.?)?', tail):
                            raise ValueError('Additional percentage-contract conditions require review')
                        scope_match = re.search(r'(?i)\ball\s+(.+?)\s+at\s+[\d.]+%', line)
                        scope = [scope_match[1].strip().lower()] if scope_match else []
                        all_items = bool(scope and scope[0] in {'items', 'products'})
                        if not scope and not all_items:
                            raise ValueError('Contract category is not established')
                        rate = decimal_number(percent[1]) / 100
                        if not 0 <= rate <= 1:
                            raise ValueError('Contract discount outside 0–100%')
                        contract.update(kind='discount', categories=scope, all_items=all_items, rate=rate)
                    else:
                        raise ValueError('Contract terms do not match a supported explicit form')
                    self.contracts.append(contract)
                except ValueError as exc:
                    self.errors.append((str(exc), source.source_id))
        if self.account and re.search(r'\bcontract\b', self.account.get('NOTE1', ''), re.I) and not self.contracts and not self.errors:
            self.errors.append(('Account refers to contract terms that were not established', self.account['source_id']))

    def price(self, sku, request_date):
        if not self.account:
            raise ValueError('Customer price band is unresolved')
        if self.errors:
            raise ValueError('; '.join(error for error, _ in self.errors))
        if sku not in self.catalogue:
            raise ValueError('SKU has no supplied pricing record')
        row = self.catalogue[sku]
        applicable = [c for c in self.contracts if (c['kind'] == 'fixed' and c['sku'] == sku)
                      or (c['kind'] == 'discount' and (c['all_items'] or row['category'].lower() in c['categories']))]
        quotes = []
        for contract in applicable:
            if ((contract['start'] and request_date < contract['start']) or
                    (contract['end'] and request_date > contract['end'])):
                raise ValueError('Recorded contract does not cover the request date; confirm current terms')
            if contract['kind'] == 'fixed':
                unit = canonical_unit(contract['unit'], row['unit'])
                quotes.append(PriceQuote(self.money(contract['amount']), unit, 'Fixed contract price; overrides band',
                                         [row['source_id'], contract['source_id']]))
            else:
                amount = self.money(decimal_number(row['list_price_gbp']) * (1 - contract['rate']))
                quotes.append(PriceQuote(amount, row['unit'],
                                         f"Contract {contract['rate'] * 100}% off list; replaces band",
                                         [row['source_id'], contract['source_id']]))
        if quotes:
            if any(q.amount < 0 for q in quotes):
                raise ValueError('Contract price cannot be negative')
            if len({q.amount for q in quotes}) != 1:
                raise ValueError('Multiple recorded contracts give conflicting prices')
            return quotes[0]
        band = self.account['PRCBND'].strip()
        column = 'list_price_gbp' if band.lower() in {'l', 'list'} else f'band_{band.lower()}_gbp'
        if column not in row or not row[column]:
            raise ValueError(f'No supplied price for account band {band!r}')
        price = self.money(row[column])
        if price < 0:
            raise ValueError('Catalogue price cannot be negative')
        return PriceQuote(price, row['unit'], f'Recorded band {band}; already net of band discount',
                          [row['source_id'], self.account['source_id']])
