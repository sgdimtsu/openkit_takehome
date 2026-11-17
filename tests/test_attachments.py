import hashlib
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytesseract

from meridian.attachments import extract_attachments, extract_pdf
from meridian.context import ContextRetriever
from meridian.inbox import load_inbox
from meridian.internal import load_internal

PACK = Path(os.environ.get('MERIDIAN_TEST_INPUT_ROOT',
                          str(Path(__file__).resolve().parents[1] / 'data')))
FAX = '05_orders/PO_GREENACRES_fax.pdf'


class AttachmentTests(unittest.TestCase):
    def test_native_pdfs_preserve_values_and_do_not_call_ocr(self):
        paths = [path for path in (PACK / '05_orders').glob('*.pdf') if path.name != Path(FAX).name]
        self.assertTrue(paths)
        for path in paths:
            before = hashlib.sha256(path.read_bytes()).digest()
            with self.subTest(path=path.name), patch('meridian.attachments._ocr') as ocr:
                result = extract_pdf(PACK, path.relative_to(PACK).as_posix())
                ocr.assert_not_called()
                self.assertTrue(result.complete)
                self.assertTrue(all(page.method == 'pdf_text' for page in result.pages))
                self.assertTrue(all(not page.source.requires_review for page in result.pages))
                self.assertEqual(before, hashlib.sha256(path.read_bytes()).digest())
        revision = extract_pdf(PACK, '05_orders/PO_HV-2291_rev.pdf')
        self.assertIn('12', revision.pages[0].source.text)
        self.assertIn('21/10/2026', revision.pages[0].source.text)
        self.assertIsNone(revision.pages[0].source.timestamp)

    @unittest.skipUnless(shutil.which('tesseract'), 'Tesseract engine required for real OCR check')
    def test_actual_two_page_fax_and_context_review_alerts(self):
        before = hashlib.sha256((PACK / FAX).read_bytes()).digest()
        result = extract_pdf(PACK, FAX)
        self.assertTrue(result.complete, result.errors)
        self.assertEqual(len(result.pages), 2)
        for index, page in enumerate(result.pages, 1):
            self.assertEqual(page.page_number, index)
            self.assertEqual(page.method, 'ocr')
            self.assertTrue(page.source.requires_review)
            self.assertTrue(page.words)
            self.assertTrue(all('confidence' in word and 'left' in word for word in page.words))
            self.assertEqual(page.source.relative_path, FAX)
            self.assertEqual(page.source.source_id, f'file:{FAX}:page:{index}')
        self.assertIn('GREENACRES', result.pages[1].source.text.upper())
        retriever = ContextRetriever(load_internal(PACK), load_inbox(PACK))
        context = retriever.prepare({'email_id': 'MSG-019'}, attachment_results=[result])
        alerts = [a for a in context['report_alerts'] if a['code'] == 'ocr_review_required']
        self.assertEqual(len(alerts), 2)
        self.assertTrue(all(alert['blocks_entry'] for alert in alerts))
        self.assertFalse(any(a['code'] == 'attachment_extraction_pending' for a in context['report_alerts']))
        self.assertEqual(before, hashlib.sha256((PACK / FAX).read_bytes()).digest())

    def test_missing_engine_and_timeout_surface_every_page(self):
        for error in (pytesseract.TesseractNotFoundError(), RuntimeError('Tesseract timeout')):
            with self.subTest(error=str(error)), patch('meridian.attachments._ocr', side_effect=error):
                result = extract_pdf(PACK, FAX)
                self.assertFalse(result.complete)
                self.assertEqual(len(result.pages), 2)
                self.assertEqual(len(result.errors), 2)
                self.assertTrue(all(page.status == 'failed' for page in result.pages))
                self.assertTrue(all(page.source.extraction_error for page in result.pages))

    def test_partial_failure_is_not_a_successful_document(self):
        with patch('meridian.attachments._ocr', side_effect=[('Page one', []), RuntimeError('unreadable')]):
            result = extract_pdf(PACK, FAX)
        self.assertFalse(result.complete)
        self.assertEqual([page.status for page in result.pages], ['extracted', 'failed'])
        retriever = ContextRetriever(load_internal(PACK), load_inbox(PACK))
        context = retriever.prepare({'email_id': 'MSG-019'}, attachment_results=[result])
        self.assertTrue(any(a['code'] == 'attachment_extraction_failed' and a['blocks_entry']
                            for a in context['report_alerts']))
        self.assertIn(result.pages[1].source.source_id, context['sources'])

    def test_empty_ocr_and_corrupt_or_missing_files_are_explicit(self):
        with patch('meridian.attachments._ocr', return_value=('', [])):
            result = extract_pdf(PACK, FAX)
        self.assertFalse(result.complete)
        self.assertIn('no readable text', result.errors[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'corrupt.pdf').write_bytes(b'not a PDF')
            for name in ('corrupt.pdf', 'missing.pdf'):
                result = extract_pdf(root, name)
                self.assertFalse(result.complete)
                self.assertTrue(result.errors)
            self.assertFalse((root / 'missing.pdf').exists())

    def test_paths_and_attachment_isolation(self):
        with self.assertRaises(ValueError):
            extract_pdf(PACK, '../outside.pdf')
        retriever = ContextRetriever(load_internal(PACK), load_inbox(PACK))
        result = extract_pdf(PACK, '05_orders/PO_HV-2291.pdf')
        with self.assertRaisesRegex(ValueError, 'outside trigger'):
            retriever.prepare({'email_id': 'MSG-019'}, attachment_results=[result])
        result = replace(result, relative_path=FAX)
        with self.assertRaisesRegex(ValueError, 'outside trigger'):
            retriever.prepare({'email_id': 'MSG-019'}, attachment_results=[result])

    def test_only_email_attachments_are_extracted_and_unsupported_types_flagged(self):
        inbox = load_inbox(PACK)
        email = next(email for email in inbox.emails if email['id'] == 'MSG-020')
        results = extract_attachments(PACK, email)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].relative_path, email['attachments'][0]['path'])
        result = extract_attachments(PACK, {'attachments': [{'path': 'unknown.txt'}]})[0]
        self.assertFalse(result.complete)
        self.assertIn('Unsupported', result.errors[0])


if __name__ == '__main__':
    unittest.main()
