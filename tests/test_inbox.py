import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from meridian.inbox import load_inbox

PACK = Path(os.environ.get("MERIDIAN_TEST_INPUT_ROOT",
                           str(Path(__file__).resolve().parents[1] / "data")))


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load(self, records):
        (self.root / 'inbox.json').write_text(json.dumps(records), encoding='utf-8')
        return load_inbox(self.root, 'inbox.json')

    def email(self, **changes):
        return {'id': 'MSG-X', 'received': '2026-10-07T08:05:00+01:00',
                'from': 'Customer', 'to': 'Orders', 'subject': 'Order',
                'body': '<script>untrusted</script>\\n', 'attachments': [],
                'in_reply_to': None, **changes}

    def test_actual_pack_preserved_and_unchanged(self):
        paths = [p for p in PACK.rglob('*') if p.is_file()
                 and p.parts[len(PACK.parts)] in
                 {'03_inbox_export', '04_reference', '05_orders', '06_orbit_export'}]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        loaded = load_inbox(PACK)
        original = json.loads((PACK / '03_inbox_export/inbox.json').read_text())
        self.assertEqual(len(loaded.emails), 45)
        self.assertFalse(loaded.warnings)
        for raw, email in zip(original, loaded.emails):
            for key in raw.keys() - {'attachments'}:
                self.assertEqual(raw[key], email[key])
            source = loaded.sources[email['source_id']]
            self.assertEqual(source.text, raw['body'])
            self.assertEqual(source.timestamp, raw['received'])
            for old, new in zip(raw['attachments'], email['attachments']):
                self.assertTrue(new['available'])
                self.assertEqual(new['path'], old['path'])
                self.assertEqual(loaded.sources[new['source_id']].relative_path, old['path'])
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.assertEqual(before, after)

    def test_generated_ids_stable_when_reordered(self):
        a = self.email(id=None)
        b = self.email(id=None, subject='Other')
        first = self.load([a, b])
        second = self.load([b, a])
        self.assertEqual([e['id'] for e in first.emails],
                         list(reversed([e['id'] for e in second.emails])))
        self.assertEqual(first.emails[0]['body'], a['body'])

    def test_duplicate_ids_fail(self):
        for identifier in ('MSG-X', None):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, 'duplicate'):
                self.load([self.email(id=identifier), self.email(id=identifier)])

    def test_bad_records_fail(self):
        for record in (None, self.email(body=7), self.email(received='yesterday'),
                       self.email(received='2026-10-07T08:05:00'),
                       self.email(attachments={}), self.email(id=10)):
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.load([record])
        with self.assertRaisesRegex(ValueError, 'JSON array'):
            self.load({})

    def test_missing_attachment_and_reply_are_visible(self):
        loaded = self.load([self.email(in_reply_to='MSG-MISSING', attachments=[
            {'filename': 'missing.pdf', 'path': 'missing.pdf'}])])
        self.assertEqual(len(loaded.warnings), 2)
        attachment = loaded.emails[0]['attachments'][0]
        self.assertFalse(attachment['available'])
        self.assertIsNone(loaded.sources[attachment['source_id']].timestamp)

    def test_shared_attachment_has_one_source(self):
        (self.root / 'po.pdf').write_bytes(b'not extracted')
        attachment = {'filename': 'po.pdf', 'path': 'po.pdf'}
        loaded = self.load([self.email(attachments=[attachment]),
                            self.email(id='MSG-Y', attachments=[attachment])])
        self.assertEqual(len(loaded.sources), 3)

    def test_paths_outside_root_rejected(self):
        for path in ('../outside.pdf', '/tmp/outside.pdf'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.load([self.email(attachments=[{'filename': 'po', 'path': path}])])
        (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.load([self.email(attachments=[{'filename': 'po', 'path': 'escape/po.pdf'}])])


if __name__ == '__main__':
    unittest.main()
