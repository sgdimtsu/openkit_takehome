"""Read PDF pages locally; preserve OCR uncertainty and every page outcome."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from urllib.parse import quote

import pypdfium2 as pdfium
import pytesseract
from pytesseract import Output

from .sources import Source, resolve_source


@dataclass
class PageExtraction:
    source: Source
    page_number: int
    method: str
    status: str
    words: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class PdfExtraction:
    relative_path: str
    pages: list[PageExtraction]
    errors: list[str]
    duration_seconds: float

    @property
    def complete(self):
        return bool(self.pages) and not self.errors and all(page.status == 'extracted' for page in self.pages)

    @property
    def sources(self):
        return [page.source for page in self.pages]


def _ocr(page, *, dpi, language, timeout):
    # PDF canvas units are 1/72 inch. Render one page at a time and close buffers.
    bitmap = page.render(scale=dpi / 72)
    try:
        image = bitmap.to_pil()
        try:
            data = pytesseract.image_to_data(image, lang=language, output_type=Output.DICT,
                                             timeout=timeout, config="--psm 11")
        finally:
            image.close()
    finally:
        bitmap.close()
    words, lines = [], {}
    for index, text in enumerate(data['text']):
        if not text.strip():
            continue
        word = {'text': text, 'confidence': float(data['conf'][index]),
                'left': data['left'][index], 'top': data['top'][index],
                'width': data['width'][index], 'height': data['height'][index]}
        words.append(word)
        line = tuple(data[name][index] for name in ('block_num', 'par_num', 'line_num'))
        lines.setdefault(line, []).append(text)
    return '\n'.join(' '.join(words) for words in lines.values()), words


def extract_pdf(root: Path, relative_path: str, *, dpi=150, language='eng', timeout=60) -> PdfExtraction:
    """Use native text for text-only pages and OCR for raster/empty pages.

    Every OCR page requires manual checking regardless of its confidence scores.
    Returned timestamps are unknown: no filesystem date is treated as freshness.
    """
    started = perf_counter()
    root = Path(root).resolve()
    path = resolve_source(root, relative_path)
    relative_path = path.relative_to(root).as_posix()
    if dpi <= 0 or timeout <= 0:
        raise ValueError('OCR dpi and timeout must be positive')
    pages, errors = [], []
    try:
        document = pdfium.PdfDocument(path)
    except Exception as exc:
        return PdfExtraction(relative_path, [], [f'Cannot open PDF: {exc}'], perf_counter() - started)
    try:
        if not len(document):
            errors.append('PDF contains no pages')
        for index in range(len(document)):
            number = index + 1
            text, words, method, error = '', [], 'pdf_text', None
            page = None
            try:
                page = document[index]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded()
                finally:
                    textpage.close()
                # A native header must not hide a scanned body on the same page.
                has_images = any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
                if has_images or not text.strip():
                    method = 'ocr'
                    native_text = text
                    try:
                        text, words = _ocr(page, dpi=dpi, language=language, timeout=timeout)
                        if text.strip() and native_text.strip():
                            text = native_text + '\n[Unverified OCR from page image]\n' + text
                        if not text.strip():
                            error = 'OCR returned no readable text; inspect the original page.'
                            text = native_text
                    except Exception as exc:
                        text = native_text
                        error = f'OCR failed: {exc}. Check Tesseract installation/language data and inspect the page.'
                if not text.strip() and not error:
                    error = 'No readable text extracted; inspect the original page.'
            except Exception as exc:
                error = f'Page extraction failed: {exc}'
            finally:
                if page is not None:
                    page.close()
            if error:
                errors.append(f'Page {number}: {error}')
            source_id = f'file:{quote(relative_path, safe="/")}:page:{number}'
            source = Source(source_id, relative_path, f'{path.name} — page {number}',
                            f'Page {number}', None, text,
                            extraction_method=method, requires_review=method == 'ocr' or bool(error),
                            extraction_error=error)
            pages.append(PageExtraction(source, number, method,
                                        'failed' if error else 'extracted', words, error))
    finally:
        document.close()
    return PdfExtraction(relative_path, pages, errors, perf_counter() - started)


def extract_attachments(root: Path, email: dict, **ocr_options) -> list[PdfExtraction]:
    """Extract only this email's attachments. Fail visibly for unsupported types."""
    results = []
    for attachment in email['attachments']:
        if Path(attachment['path']).suffix.lower() != '.pdf':
            results.append(PdfExtraction(attachment['path'], [],
                                        ['Unsupported attachment type; content not extracted.'], 0.0))
        else:
            results.append(extract_pdf(root, attachment['path'], **ocr_options))
    return results
