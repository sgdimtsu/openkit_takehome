"""Read internal snapshots once; retain raw values and traceable evidence."""
import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .sources import Source, add_source, resolve_source


@dataclass
class InternalData:
    documents: dict[str, str]
    open_items: list[dict]
    prices_by_sku: dict[str, dict]
    customers_by_id: dict[str, dict]
    stock_by_sku: dict[str, dict]
    orders_by_id: dict[int, dict]
    orders_by_customer: dict[str, list[dict]]
    lines_by_order: dict[int, list[dict]]
    parameters: dict[str, dict]
    sources: dict[str, Source]
    warnings: list[str]


# The export declares no primary keys. These logical keys are needed for
# evidence IDs and lookups; all other columns are discovered from SQLite.
KEYS = {'CUST': ('CUSNO',), 'STK': ('STKCD',), 'OHDR': ('ORDNO',),
        'OLIN': ('ORDNO', 'LNNO'), 'SYSPARM': ('PKEY',)}


def _field(row, name, label):
    if name not in row:
        raise ValueError(f'{label}: missing required field {name}')
    return row[name]


def _csv(root, relative, sources):
    path = resolve_source(root, relative)
    rows = []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if (not reader.fieldnames or any(not name.strip() for name in reader.fieldnames)
                or len(reader.fieldnames) != len(set(reader.fieldnames))):
            raise ValueError(f'{relative}: missing, blank or duplicate CSV headers')
        if 'source_id' in reader.fieldnames:
            raise ValueError(f'{relative}: source_id is reserved for evidence metadata')
        for number, raw in enumerate(reader, 2):
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f'{relative}: malformed CSV record {number}')
            source_id = f'csv:{quote(relative, safe="/")}:record:{number}'
            add_source(sources, Source(source_id, relative, f'{path.name} — record {number}',
                                      f'CSV record {number} (header is record 1)', None,
                                      json.dumps(raw, ensure_ascii=False)))
            rows.append({**raw, 'source_id': source_id})
    return rows


def _keyed(rows, key, label):
    result = {}
    for row in rows:
        value = _field(row, key, label)
        if value is None or value == '' or value in result:
            raise ValueError(f'{label}: missing or duplicate {key}: {value!r}')
        result[value] = row
    return result


def load_orbit(root: Path, relative: str, sources: dict) -> dict[str, list[dict]]:
    """Open an existing SQLite file read-only, with writes also denied by SQLite."""
    path = resolve_source(root, relative)
    if not path.is_file():
        raise FileNotFoundError(f'Orbit snapshot not found: {path}')
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            connection.execute('PRAGMA query_only = ON')
            connection.execute('BEGIN')
            connection.row_factory = sqlite3.Row
            tables = {}
            available = {row['name'] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table, keys in KEYS.items():
                if table not in available:
                    raise ValueError(f'Orbit: missing required table {table}')
                # Table names come from the small logical-key mapping, not input text.
                columns = {row['name'] for row in connection.execute(
                    f'PRAGMA table_info("{table}")')}
                missing = set(keys) - columns
                if missing:
                    raise ValueError(f'Orbit {table}: missing key columns {sorted(missing)}')
                if 'source_id' in columns:
                    raise ValueError(f'Orbit {table}: source_id is reserved for evidence metadata')
                tables[table] = [dict(row) for row in
                                 connection.execute(f'SELECT * FROM "{table}"')]

        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError(f'Cannot read Orbit snapshot {relative}: {exc}') from exc
    timestamp_rows = [row for row in tables['SYSPARM'] if row['PKEY'] == 'LASTEXP']
    timestamp = (_field(timestamp_rows[0], 'PVAL', 'Orbit SYSPARM LASTEXP')
                 if len(timestamp_rows) == 1 else None)
    for table, rows in tables.items():
        seen = set()
        for row in rows:
            key = tuple(row[column] for column in KEYS[table])
            if any(value is None or value == '' for value in key) or key in seen:
                raise ValueError(f'Orbit {table}: missing or duplicate key {key!r}')
            seen.add(key)
            locator = ', '.join(f'{column}={row[column]}' for column in KEYS[table])
            source_id = 'orbit:' + quote(relative, safe='/') + ':' + table + ':' + ':'.join(
                quote(str(value), safe='') for value in key)
            add_source(sources, Source(source_id, relative, f'Orbit {table} — {locator}',
                                      f'{table}: {locator}', timestamp,
                                      json.dumps(row, ensure_ascii=False)))
            row['source_id'] = source_id
    return tables


def load_internal(root: Path, *,
                  open_items_path='02_open_items_log.csv',
                  notes_path='04_reference/customer_account_notes.md',
                  prices_path='04_reference/price_list.csv',
                  templates_path='04_reference/email_templates.md',
                  orbit_path='06_orbit_export/orbit_nightly.sqlite') -> InternalData:
    root = Path(root).resolve()
    sources = {}
    documents = {}
    for name, relative in [('account_notes', notes_path), ('email_templates', templates_path)]:
        path = resolve_source(root, relative)
        text = path.read_text(encoding='utf-8')
        source_id = 'file:' + quote(relative, safe='/')
        add_source(sources, Source(source_id, relative, path.name, 'Whole document', None, text))
        documents[name] = source_id
    open_items = _csv(root, open_items_path, sources)
    prices = _keyed(_csv(root, prices_path, sources), 'sku', 'Price list')
    tables = load_orbit(root, orbit_path, sources)
    customers = _keyed(tables['CUST'], 'CUSNO', 'Orbit customers')
    stock = _keyed(tables['STK'], 'STKCD', 'Orbit stock')
    orders = _keyed(tables['OHDR'], 'ORDNO', 'Orbit orders')
    parameters = _keyed(tables['SYSPARM'], 'PKEY', 'Orbit parameters')
    by_customer = {}
    lines = {}
    warnings = ['Reference file freshness is unknown; CSV dates are retained as raw record values.',
                'Orbit LASTEXP is a raw export timestamp; its timezone is not supplied.',
                'Account notes contain their own freshness limitations; spreadsheet Who is unverified.']
    if not parameters.get('LASTEXP', {}).get('PVAL'):
        warnings.append('Orbit snapshot freshness is unknown: LASTEXP is missing or empty.')
    for order in orders.values():
        customer_id = _field(order, 'CUSNO', f"Orbit order {order['ORDNO']}")
        by_customer.setdefault(customer_id, []).append(order)
        if customer_id not in customers:
            warnings.append(f"Order {order['ORDNO']}: unknown customer {order['CUSNO']}")
    for line in tables['OLIN']:
        lines.setdefault(line['ORDNO'], []).append(line)
        if line['ORDNO'] not in orders:
            warnings.append(f"Order line {line['ORDNO']}/{line['LNNO']}: missing order")
        stock_id = _field(line, 'STKCD', f"Orbit line {line['ORDNO']}/{line['LNNO']}")
        if stock_id not in stock:
            warnings.append(f"Order line {line['ORDNO']}/{line['LNNO']}: unknown stock {line['STKCD']}")
    return InternalData(documents, open_items, prices, customers, stock, orders,
                        by_customer, lines, parameters, sources, warnings)
