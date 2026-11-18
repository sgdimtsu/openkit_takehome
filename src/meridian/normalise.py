"""Explicit numerical/date normalisation; no float arithmetic for money."""
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .context import order_date

CENT = Decimal('0.01')
VAT_RATE = Decimal('0.20')  # User-approved prototype rule; export conflicts are surfaced.


def decimal_number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('A numeric value is required')
    text = str(value).strip().replace('£', '').strip()
    if not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', text):
        raise ValueError(f'Unrecognised numeric value: {value!r}')
    try:
        number = Decimal(text.replace(',', ''))
    except InvalidOperation as exc:
        raise ValueError('Invalid numeric value') from exc
    if not number.is_finite():
        raise ValueError('Numeric values must be finite')
    return number


def money(value):
    return decimal_number(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_text(value):
    return format(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def quantity(value, *, allow_zero=False):
    result = decimal_number(value)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError('Quantity must be positive')
    return result


def discount_rate(value):
    text = str(value).strip()
    if text.endswith('%'):
        rate = decimal_number(text[:-1]) / 100
    else:
        number = decimal_number(value)
        if number == 1:
            raise ValueError('Discount 1 is ambiguous between 1% and a fraction of 1')
        rate = number if number < 1 else number / 100
    if not 0 <= rate <= 1:
        raise ValueError('Discount must be between 0% and 100%')
    return rate


def orbit_net_price(line):
    # UPRC is pence before the separately recorded DISC, as approved by the user.
    base = decimal_number(line['UPRC']) / 100
    if base < 0:
        raise ValueError('Recorded price cannot be negative')
    return money(base * (1 - discount_rate(line['DISC'])))


def exclusive_amount(amount, vat_basis):
    value = decimal_number(amount)
    if value < 0:
        raise ValueError('Quoted amount cannot be negative')
    if vat_basis == 'inclusive':
        return money(value / (1 + VAT_RATE))
    if vat_basis == 'exclusive':
        return money(value)
    raise ValueError('Quoted VAT basis is unknown')


def requested_date(value):
    """Preserve deadlines/windows. Do not turn relative wording into a commitment."""
    raw = str(value).strip()
    kind, date_text = 'exact_date', raw
    for pattern, candidate in ((r'(?i)^w/c\s+', 'week_commencing'),
                               (r'(?i)^before\s+', 'before'),
                               (r'(?i)^(?:by|on)\s+', 'by' if raw.lower().startswith('by') else 'exact_date')):
        if re.search(pattern, raw):
            date_text = re.sub(pattern, '', raw).strip()
            kind = candidate
            break
    # Requested dates must state a year; Excel serials are for recorded Orbit dates only.
    if not (re.fullmatch(r'\d{8}', date_text) or re.search(r'\b\d{4}\b', date_text)
            or re.fullmatch(r'\d{1,2}/\d{1,2}/\d{2}', date_text)):
        raise ValueError('Date/month/year or relative-date meaning needs confirmation')
    try:
        parsed = order_date(date_text)
    except ValueError:
        parsed = None
        for fmt in ('%d %B %Y', '%d %b %Y'):
            try:
                parsed = datetime.strptime(date_text, fmt).date()
                break
            except ValueError:
                pass
        if parsed is None:
            raise ValueError(f'Unrecognised requested date: {raw!r}')
    result = {'raw': raw, 'kind': kind, 'date': parsed.isoformat()}
    if kind == 'week_commencing':
        if parsed.weekday() != 0:
            raise ValueError('Week-commencing date is not a Monday; confirm the intended window')
        result['end_date'] = (parsed + timedelta(days=6)).isoformat()
    return result


def canonical_unit(raw, expected, *, description=None):
    """Handle ordinary count-unit plurals; do not multiply pack sizes or convert units."""
    first = re.findall(r'[a-z]+', str(raw).lower())
    if not first:
        raise ValueError('Unit is missing')
    word = first[0]
    canonical = str(expected).lower().strip()
    plurals = {canonical, canonical + 's'}
    if canonical.endswith(('s', 'x', 'ch', 'sh')):
        plurals.add(canonical + 'es')
    if canonical == 'each':
        plurals.update({'ea', 'unit', 'units'})
    if word not in plurals:
        raise ValueError(f'Requested unit {raw!r} does not establish catalogue unit {expected!r}')
    counts = re.findall(r'\d+', str(raw))
    if counts:
        pack = re.search(r'(?i)\b' + re.escape(canonical) + r'\s+(?:of\s+)?(\d+)\b', description or '')
        if pack is None or counts != [pack[1]]:
            raise ValueError('Requested pack size does not establish the catalogue pack; no conversion is applied')
    return canonical
