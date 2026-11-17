import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meridian.internal import KEYS, _csv, _keyed, load_internal, load_orbit

PACK = Path(os.environ.get('MERIDIAN_TEST_INPUT_ROOT',
                          str(Path(__file__).resolve().parents[1] / 'data')))


class InternalTests(unittest.TestCase):
    def test_supplied_data_values_indexes_evidence_and_no_writes(self):
        paths = [p for p in PACK.rglob('*') if p.is_file()]
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in paths}
        loaded = load_internal(PACK)
        self.assertEqual(len(loaded.customers_by_id), 60)
        self.assertEqual(len(loaded.orders_by_id), 7198)
        self.assertEqual(sum(map(len, loaded.lines_by_order.values())), 20493)
        self.assertEqual(loaded.parameters['VATRT']['PVAL'], '17.5')
        self.assertEqual(loaded.parameters['LASTEXP']['PVAL'], '20261008 231502')
        self.assertEqual(loaded.prices_by_sku['CON-BR-6']['list_price_gbp'], '18.95')
        self.assertTrue(any(row['Who'] == '?' for row in loaded.open_items))
        for account, orders in loaded.orders_by_customer.items():
            for order in orders:
                self.assertEqual(order['CUSNO'], account)
                self.assertIs(order, loaded.orders_by_id[order['ORDNO']])
        discounts = {line['DISC'] for lines in loaded.lines_by_order.values() for line in lines}
        self.assertTrue({0.05, 5.0}.issubset(discounts))
        for source in loaded.sources.values():
            self.assertTrue((PACK / source.relative_path).is_file())
            self.assertTrue(source.locator)
            self.assertTrue(source.text)
        first = next(iter(loaded.orders_by_id.values()))
        source = loaded.sources[first['source_id']]
        self.assertIn(str(first['ORDNO']), source.locator)
        self.assertEqual(source.timestamp, '20261008 231502')
        self.assertTrue(all(loaded.sources[r['source_id']].timestamp is None
                            for r in loaded.open_items))
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).digest() for p in paths})
        self.assertEqual(set(paths), {p for p in PACK.rglob('*') if p.is_file()})

    def test_connection_rejects_writes_and_creates_no_indexes(self):
        connect = sqlite3.connect
        traces = []

        def checked_connect(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith('?mode=ro'))
            self.assertTrue(kwargs['uri'])
            connection = connect(database_uri, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('CREATE TABLE forbidden_write (x)')
            connection.set_trace_callback(traces.append)
            return connection

        with patch('meridian.internal.sqlite3.connect', side_effect=checked_connect):
            load_orbit(PACK, '06_orbit_export/orbit_nightly.sqlite', {})
        self.assertIn('PRAGMA query_only = ON', traces)
        self.assertFalse(any('CREATE INDEX' in statement.upper() for statement in traces))

    def test_source_ids_stable_across_loads(self):
        first = load_internal(PACK)
        second = load_internal(PACK)
        self.assertEqual(first.sources, second.sources)

    def test_missing_database_not_created_and_invalid_schema_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                load_orbit(root, 'missing.sqlite', {})
            self.assertFalse((root / 'missing.sqlite').exists())
            connection = sqlite3.connect(root / 'empty.sqlite')
            connection.close()
            with self.assertRaisesRegex(ValueError, 'missing required table'):
                load_orbit(root, 'empty.sqlite', {})

    def test_dynamic_csv_headers_and_structural_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'items.csv'
            path.write_text('Custom,Future column\nexample,preserved\n')
            sources = {}
            rows = _csv(root, 'items.csv', sources)
            self.assertEqual(rows[0]['Future column'], 'preserved')
            self.assertIn('Future column', sources[rows[0]['source_id']].text)
            for text in ('A,A\n1,2\n', ',A\n1,2\n', '',
                         'source_id,A\noriginal,2\n', 'A,B\n1\n', 'A,B\n1,2,3\n'):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    path.write_text(text)
                    _csv(root, 'items.csv', {})

    def test_dynamic_orbit_columns_preserved_without_full_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = sqlite3.connect(root / 'snapshot.sqlite')
            for table, keys in KEYS.items():
                definitions = ', '.join(f'"{key}" TEXT' for key in keys)
                connection.execute(f'CREATE TABLE "{table}" ({definitions}, "EXTRA" TEXT)')
            connection.execute("INSERT INTO CUST VALUES ('TEST01', 'new field value')")
            connection.commit()
            connection.close()
            sources = {}
            tables = load_orbit(root, 'snapshot.sqlite', sources)
            row = tables['CUST'][0]
            self.assertEqual(row['EXTRA'], 'new field value')
            self.assertIn('new field value', sources[row['source_id']].text)
            self.assertIsNone(sources[row['source_id']].timestamp)
            # Existing evidence IDs survive additions to the input schema.
            connection = sqlite3.connect(root / 'snapshot.sqlite')
            connection.execute('ALTER TABLE CUST ADD COLUMN ANOTHER TEXT')
            connection.commit()
            connection.close()
            changed = load_orbit(root, 'snapshot.sqlite', {})['CUST'][0]
            self.assertEqual(changed['source_id'], row['source_id'])
            self.assertIn('ANOTHER', changed)

    def test_required_business_field_checked_when_indexing(self):
        with self.assertRaisesRegex(ValueError, 'Price list: missing required field sku'):
            _keyed([{'custom': 'value'}], 'sku', 'Price list')


if __name__ == '__main__':
    unittest.main()
