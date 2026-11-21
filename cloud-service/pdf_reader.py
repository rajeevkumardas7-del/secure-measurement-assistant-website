"""Bounded, temporary PDF extraction using the desktop estimate parser."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

MAX_BYTES = 25 * 1024 * 1024
SLOT = threading.BoundedSemaphore(1)

class PdfReadError(ValueError):
    pass

def read_upload(raw, mode='custom_ai'):
    if not raw.startswith(b'%PDF-') or len(raw) > MAX_BYTES:
        raise PdfReadError('Choose a valid PDF no larger than 25 MB.')
    if mode not in ('custom_ai', 'standard'):
        raise PdfReadError('Choose a supported PDF reader.')
    if not SLOT.acquire(blocking=False):
        raise PdfReadError('PDF reader is busy. Please retry shortly.')
    try:
        with tempfile.TemporaryDirectory(prefix='secure-pdf-') as folder:
            source = Path(folder) / 'input.pdf'
            source.write_bytes(raw)
            try:
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(source), mode],
                    capture_output=True, timeout=60, check=False)
            except subprocess.TimeoutExpired:
                raise PdfReadError('PDF exceeded the 60-second reading limit.') from None
            if run.returncode or len(run.stdout) > 4 * 1024 * 1024:
                raise PdfReadError('PDF could not be read within the preview limits.')
            try:
                result = json.loads(run.stdout)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise PdfReadError('PDF reader returned an invalid result. Try the desktop application.') from None
            if 'error' in result:
                raise PdfReadError(result['error'])
            return result
    except PdfReadError:
        raise
    except Exception:
        raise PdfReadError('The PDF reader stopped unexpectedly. Try the desktop application.') from None
    finally:
        SLOT.release()

def extract(path, mode='custom_ai'):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(path)) as document:
        if not 1 <= len(document) <= 40:
            raise ValueError('Preview supports 1–40 pages.')
    from estimate import read_pdf, read_pdf_custom_ai
    catalog = json.loads(Path(__file__).with_name('reader-catalog.json').read_text(encoding='utf-8'))
    items = read_pdf_custom_ai(path, catalog=catalog) if mode == 'custom_ai' else read_pdf(path, catalog=catalog)
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
        result = extract(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'custom_ai')
    except ValueError as error:
        result = {'error': str(error)}
    except Exception:
        result = {'error': 'Unsupported, encrypted or damaged PDF. No measurements were imported.'}
    print(json.dumps(result, default=str))
