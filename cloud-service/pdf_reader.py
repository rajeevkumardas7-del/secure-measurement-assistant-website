"""Bounded, temporary PDF extraction using the desktop estimate parser."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

MAX_BYTES = 5 * 1024 * 1024
SLOT = threading.BoundedSemaphore(1)

def read_upload(raw):
    if not raw.startswith(b'%PDF-') or len(raw) > MAX_BYTES:
        raise ValueError('Choose a PDF no larger than 5 MB.')
    if not SLOT.acquire(blocking=False):
        raise RuntimeError('PDF reader is busy. Please retry shortly.')
    try:
        with tempfile.TemporaryDirectory(prefix='secure-pdf-') as folder:
            source = Path(folder) / 'input.pdf'
            source.write_bytes(raw)
            try:
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(source)],
                    capture_output=True, timeout=60, check=False)
            except subprocess.TimeoutExpired:
                raise ValueError('PDF exceeded the 60-second reading limit.') from None
            if run.returncode or len(run.stdout) > 4 * 1024 * 1024:
                raise ValueError('PDF could not be read within the preview limits.')
            result = json.loads(run.stdout)
            if 'error' in result:
                raise ValueError(result['error'])
            return result
    finally:
        SLOT.release()

def extract(path):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(path)) as document:
        if not 1 <= len(document) <= 40:
            raise ValueError('Preview supports 1–40 pages.')
        for page in document:
            try:
                text = page.get_textpage()
                try:
                    if not text.get_text_range().strip():
                        raise ValueError('This PDF needs OCR. Scanned PDFs are not enabled in the web preview.')
                finally:
                    text.close()
            finally:
                page.close()
    from estimate import read_pdf
    items = read_pdf(path)
    if not items:
        raise ValueError('No supported estimate items found. Review this PDF in the desktop application.')
    if len(items) > 500 or sum(len(item.get('rows', [])) for item in items) > 5000:
        raise ValueError('Estimate exceeds preview row limits.')
    return {'items': list(items), 'reviewRequired': True,
            'notice': 'Review every item and measurement against the PDF. Nothing has been entered in the portal.'}

if __name__ == '__main__':
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (320*1024*1024, 320*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    try:
        result = extract(sys.argv[1])
    except ValueError as error:
        result = {'error': str(error)}
    except Exception:
        result = {'error': 'Unsupported, encrypted or damaged PDF. No measurements were imported.'}
    print(json.dumps(result, default=str))
