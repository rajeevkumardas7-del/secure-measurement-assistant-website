"""Bounded, temporary PDF extraction using the desktop estimate parser."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

MAX_BYTES = 25 * 1024 * 1024
SLOT = threading.BoundedSemaphore(1)
MAX_READER_KB = 384 * 1024

class PdfReadError(ValueError):
    pass


def reader_memory_kb(pid):
    if os.name != 'posix':
        return 0
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])
    except FileNotFoundError:
        return 0  # The reader exited between polling and reading its status.
    return 0


def run_reader(source, mode):
    # OCR maps large libraries; bound resident memory instead of virtual mappings.
    with subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(source), mode],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        deadline = time.monotonic() + 60
        try:
            while True:
                if reader_memory_kb(process.pid) > MAX_READER_KB:
                    raise PdfReadError('PDF exceeds the hosted reader memory limit. Split it into smaller PDFs.')
                if time.monotonic() >= deadline:
                    raise PdfReadError('PDF exceeded the 60-second reading limit.')
                try:
                    output, errors = process.communicate(timeout=.25)
                    return subprocess.CompletedProcess(process.args, process.returncode, output, errors)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

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
            run = run_reader(source, mode)
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
        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    try:
        result = extract(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'custom_ai')
    except ValueError as error:
        result = {'error': str(error)}
    except Exception:
        result = {'error': 'Unsupported, encrypted or damaged PDF. No measurements were imported.'}
    print(json.dumps(result, default=str))
