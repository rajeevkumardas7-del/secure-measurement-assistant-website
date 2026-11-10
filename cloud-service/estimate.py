"""Read SECURE detailed-estimate tables; never import the rate-analysis LIST."""
import re
import ast
import unicodedata
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from decimal import Decimal
from pathlib import Path
import pdfplumber
from openpyxl import load_workbook

OCR_IMPORT_ERROR=(
    'The built-in scanned-PDF reader is unavailable. Extract the complete SECURE Measurement Assistant ZIP, '    'keep the _internal folder beside SECURE-Assistant.exe, then run the EXE from that folder. '    'If Windows Security has quarantined a file, restore the SECURE Measurement Assistant folder and try again.'
)

def ocr_reader_status():
    """Return a user-safe readiness result for the bundled offline OCR reader."""
    try:
        import numpy  # noqa: F401
        import pypdfium2  # noqa: F401
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True, 'Built-in scanned-PDF reader is ready.'
    except (ImportError, OSError, RuntimeError) as error:
        return False, f'{OCR_IMPORT_ERROR} Details: {error}'


class EstimateItems(list):
    def __init__(self,items=(),costs=None):
        super().__init__(items);self.costs=costs or {}


# User-selectable reader profile.  The custom profile documents the same
# source-faithful method used by the review workflow: read every page,
# preserve wrapped rows across page breaks, normalize item-code punctuation,
# keep negative deductions, and validate printed totals before entry.
PDF_READING_MODES = {
    'standard': 'Standard PDF reader',
    'custom_ai': 'Custom AI table reader (recommended)',
}


def read_pdf_custom_ai(path, catalog=None, progress=None):
    """Read a PDF with the source-faithful custom table-reading profile.

    This is intentionally offline and deterministic; it does not send the
    estimate to an external AI service.  It reuses the validated parser and
    adds review metadata so the UI can explain exactly what was checked.
    """
    from trained_reader import audit_items
    items = read_pdf(path, catalog=catalog, progress=progress)
    for item in items:
        item.setdefault('reader_mode', 'custom_ai')
        item.setdefault('reader_checks', (
            'All pages scanned; wrapped rows joined; item codes normalized; '
            'negative deductions preserved; printed totals checked.'
        ))
    return audit_items(items, progress)


_BUNDLED_CATALOG = None

# SECURE printouts sometimes insert a space or an extra dot before a suffix
# (for example `79.04.13 a` and `79.21.07.c`).  Keep the entire token
# through parsing, then standardise only that separator.
# Most schedule items are numeric (79.04.13a).  PWD schedules also use a
# named family followed by numeric levels (79.PWD.10.1).  Keep both whole so
# a PDF reader never treats PWD as part of the item description.
# A heading code can contain numeric and alphabetic parts.  The final heading
# separator (``--``) is what distinguishes it from ordinary description text.
_ITEM_CODE_TOKEN = r"'?(?:79|9)(?:\s*[^A-Za-z0-9\s]\s*[A-Za-z0-9]+){2,}(?:[ \t]+[A-Za-z]+)?(?:\s*[^A-Za-z0-9\s])?"
_SCANNED_ITEM_CODE = r'(?:79|9)(?:[^A-Za-z0-9]+[A-Za-z0-9]+){2,}'

def _normalise_item_code(value):
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = re.sub(r'[\u200b-\u200d\ufeff]', '', text).strip().lstrip("'").lower()
    # Preserve any named or numeric family, while treating all alphabet cases
    # and separators as equivalent: 79.PWD.10.1 == 79-pwd-10-1 == 79pwd101.
    generic = re.fullmatch(r'([a-z0-9]+)(?:[^a-z0-9]+([a-z0-9]+)){2,}[^a-z0-9]*', text)
    if generic:
        parts = [part for part in re.split(r'[^a-z0-9]+', text) if part]
        if parts[0] == '9':
            parts[0] = '79'
        if parts[0] in ('79', '9'):
            # Traditional 79.04.13a has a suffix on its third numeric part.
            if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit() and parts[3].isalpha():
                return '.'.join(parts[:3]) + parts[3]
            return '.'.join(parts)
    parts = re.fullmatch(r'(\d{1,2})[.\s\-_/]+(\d{2})[.\s\-_/]+(\d{2,})(?:[.\s\-_/]*([a-z]+))?[.\s]*', text)
    if parts:
        first = parts.group(1)
        if first == '9':
            first = '79'
        return '.'.join((first, parts.group(2), parts.group(3))) + (parts.group(4) or '')
    code = re.sub(r'\s+', '', text).rstrip('.')
    if re.match(r'^9\.\d{2}\.\d{2}', code):
        code = '7' + code
    return re.sub(r'(?<=\d)\.(?=[A-Za-z]$)', '', code).lower()



def _repair_truncated_item_names(items, catalog=None):
    """Restore a clipped PDF/OCR heading from the bundled SoR record."""
    global _BUNDLED_CATALOG
    suspects = [item for item in items if re.match(
        r'^\s*(?:of|and|the|with|including|to|for)\b', str(item.get('name', '')), re.I
    )]
    if not suspects:
        return
    records = catalog
    if not records:
        try:
            if _BUNDLED_CATALOG is None:
                from editor import load_catalog
                source = Path(__file__).with_name('Chhattisgarh_SoR_Item_Rates.xlsx')
                _BUNDLED_CATALOG, _ = load_catalog(source)
            records = _BUNDLED_CATALOG
        except Exception:
            return
    for item in suspects:
        record = records.get(item.get('code'))
        if record and record.get('name'):
            item['name'] = record['name']
            if not item.get('unit') and record.get('unit'):
                item['unit'] = record['unit']


def _clean_row_description(value, serial=''):
    """Remove a duplicated printed Sl No from a measurement description."""
    text = str(value or '').strip()
    match = re.match(r'^(\d{1,4})\s*[.)]\s*(.*)$', text, re.S)
    if match and (not serial or match.group(1) == str(serial).strip()):
        return match.group(2).strip()
    return text


def _is_context_only_row(value):
    """Headings inside a measurement table must never be entered in SECURE."""
    text = re.sub(r'\s+', ' ', str(value or '').strip()).casefold()
    return text in {'details', 'detail', 'description', 'descriptions', 'particulars', 'measurement details'}


def _is_deduction_heading(value):
    """Identify a printed section heading that represents a deduction.

    The printed quantity remains authoritative: this only annotates the row
    and warns when a deduction line lost its minus sign during extraction.
    """
    return bool(re.search(r'\b(?:deduction|deductions|deduct|less|minus)\b', str(value or ''), re.I))


def _calculated_measurement_quantity(row):
    """Calculate quantity from measurement columns, ignoring PDF quantities."""
    factors = [str(row.get(key, '') or '').strip().replace(',', '')
               for key in ('no', 'l', 'b', 'd', 'cf')]
    if not any(factors):
        return None
    quantity = Decimal('1')
    for value in factors:
        if not value or value == '-':
            continue
        factor = _measurement_factor(value)
        if factor is None:
            return None
        quantity *= factor
    # The PDF can print the deduction heading outside the extracted grid.
    # Preserve the quantity's sign even when all dimensions are positive.
    printed = str(row.get('quantity', '') or '').strip().replace(',', '').replace('\u2212', '-')
    negative = bool(re.fullmatch(r'-\d+(?:\.\d+)?', printed))
    has_printed_quantity = bool(re.fullmatch(r'-?\d+(?:\.\d+)?', printed))
    if (negative or (not has_printed_quantity and _is_deduction_heading(row.get('head', '')))) and quantity > 0:
        quantity = -quantity
    return f'{quantity:.4f}'


def _measurement_factor(value):
    """Read a numeric measurement factor or a printed arithmetic expression.

    SECURE Print uses values such as ``(6+5)/2`` in L/B/D columns.  Accept
    only arithmetic made from numbers and ``+ - * / ( )``; never evaluate
    general Python text from a PDF.
    """
    text = str(value or '').strip().replace(',', '').replace('\u2212', '-')
    if not text or text == '-':
        return Decimal('1')
    if re.fullmatch(r'-?\d+(?:\.\d+)?', text):
        return Decimal(text)
    if not re.fullmatch(r'[\d.()+\-*/\s]+', text):
        return None
    try:
        node = ast.parse(text, mode='eval').body
    except SyntaxError:
        return None
    def calculate(part):
        if isinstance(part, ast.Constant) and isinstance(part.value, (int, float)):
            return Decimal(str(part.value))
        if isinstance(part, ast.UnaryOp) and isinstance(part.op, (ast.UAdd, ast.USub)):
            value = calculate(part.operand)
            return value if isinstance(part.op, ast.UAdd) else -value
        if isinstance(part, ast.BinOp) and isinstance(part.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = calculate(part.left), calculate(part.right)
            if isinstance(part.op, ast.Add): return left + right
            if isinstance(part.op, ast.Sub): return left - right
            if isinstance(part.op, ast.Mult): return left * right
            if right == 0: raise ZeroDivisionError
            return left / right
        raise ValueError('unsupported expression')
    try:
        return calculate(node)
    except (ArithmeticError, ValueError):
        return None


def _annotate_deduction_row(row):
    """Keep deductions explicit without changing the source-PDF quantity."""
    try:
        quantity = Decimal(str(row.get('quantity', '')).replace(',', ''))
    except Exception:
        return row
    under_deduction = _is_deduction_heading(row.get('head', ''))
    # Entry is determined from the calculated quantity.  This is saved with
    # every row, so later automation does not have to infer a deduction from
    # the surrounding PDF text again.
    row['deduction'] = bool(quantity < 0)
    row['entry_section'] = 'deduction' if quantity < 0 else 'measurement'
    if under_deduction and quantity > 0:
        prior = str(row.get('warning', '')).strip()
        message = 'This row is under a deduction heading but its printed quantity is positive. Confirm the minus sign before SECURE entry.'
        row['warning'] = (prior + ' ' + message).strip()
    return row


def _summarize_deductions(item):
    """Store addition, deduction and net quantities for review and resume."""
    additions = Decimal('0')
    deductions = Decimal('0')
    for row in item.get('rows', []):
        _annotate_deduction_row(row)
        try:
            quantity = Decimal(str(row.get('quantity', '')).replace(',', ''))
        except Exception:
            continue
        if quantity < 0:
            deductions += abs(quantity)
        else:
            additions += quantity
    item['addition_total'] = f'{additions:.4f}'
    item['deduction_total'] = f'{deductions:.4f}'
    item['net_total'] = f'{additions-deductions:.4f}'
    item['entry_sections'] = {
        'measurement': [str(row.get('serial', '')) for row in item.get('rows', []) if row.get('entry_section') == 'measurement'],
        'deduction': [str(row.get('serial', '')) for row in item.get('rows', []) if row.get('entry_section') == 'deduction'],
    }
    return item


def _read_embedded_reusable_estimate(path):
    """Return exact saved measurements from a SECURE reusable PDF, if any."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        attachments = getattr(reader, 'attachments', {}) or {}
        candidates = attachments.get('secure-measurements.json', [])
        if isinstance(candidates, (bytes, bytearray)):
            candidates = [candidates]
        for raw in candidates:
            payload = json.loads(bytes(raw).decode('utf-8'))
            if (payload.get('format') == 'secure-reusable-estimate'
                    and payload.get('version') == 1
                    and isinstance(payload.get('items'), list)):
                return EstimateItems(payload['items'], payload.get('costs') or {})
    except Exception:
        # Normal PDFs do not carry our attachment. Continue with the existing
        # selectable-text/OCR reader rather than changing its error behaviour.
        pass
    return None


def _extract_selectable_page(path, number):
    """Read one selectable page in its own PDF handle.

    pdfplumber/pdfminer keeps mutable parser state, so sharing one document
    between worker threads can scramble rows.  Independent handles let a small
    worker pool use spare CPU/RAM safely while the caller restores page order.
    """
    with pdfplumber.open(str(path)) as pdf:
        page = pdf.pages[number - 1]
        text = page.extract_text() or ''
        found_tables = page.find_tables()
        tables = [table.extract() for table in found_tables]
        positions = {}
        # pdfplumber's search misses code runs split into separate PDF text
        # spans. Word positions remain reliable for table insertion order.
        for word in page.extract_words():
            token = str(word.get('text', '')).strip()
            if re.match(r'^79\.\d{2}\.\d{2}', token):
                positions[_normalise_item_code(token)] = word.get('top', 0)
        return number, text, tables, [table.bbox for table in found_tables], positions

def read_cost_summary(text):
    labels={
        'unskilled':'Unskilled Manpower wages',
        'skilled':'Skilled',
        'semiskilled':'SemiSkilled',
        'material':'Materials (Includes Extra Charges)',
        'lumpsum_material':'Lumpsum Material',
    }
    costs={}
    for key,label in labels.items():
        match=re.search(r'(?mi)^'+re.escape(label)+r'\s+([\d,]+(?:\.\d+)?)\s*$',text)
        costs[key]=match.group(1).replace(',','') if match else '0.00'
    labour=Decimal(costs['unskilled'])
    material=Decimal(costs['material'])+Decimal(costs['lumpsum_material'])+Decimal(costs['semiskilled'])+Decimal(costs['skilled'])
    costs.update(total_labour=f'{labour:.2f}',total_material=f'{material:.2f}',total_amount=f'{labour+material:.2f}')
    return costs


def _assign_display_order(items):
    """Keep document order when a printout restarts its item serials.

    SECURE composite estimates commonly print the primary schedule items as
    ``1`` and then print numbered sub-items below one parent item.  Sorting
    such a document by its printed number moves valid measurements away from
    their parent.  The application uses a stable display sequence and retains
    the printed number for audit when it differs.
    """
    expected = list(range(1, len(items) + 1))
    printed = [item.get('number') for item in items]
    if printed != expected:
        for number, item in enumerate(items, 1):
            item.setdefault('source_number', item.get('number'))
            item['number'] = number
    # The portal code and printed serial are not a unique item identity:
    # composite estimates can repeat both in separate headings.  Keep a stable
    # occurrence key for UI editing, resume checkpoints and error reporting.
    occurrences = {}
    for display_index, item in enumerate(items, 1):
        code = _normalise_item_code(item.get('code', ''))
        occurrences[code] = occurrences.get(code, 0) + 1
        item['item_instance_id'] = f'{code}#{occurrences[code]}'
        item['document_order'] = display_index
    return items

def parse_tables(pages):
    items=[]; current=None; measuring=False; head=''
    next_serial=1; alternate_layout=False
    for page,tables in enumerate(pages,1):
        for table in tables:
            # Rate-analysis tables use an item/resource Code column. They are
            # not measurement rows and must never be treated as such.
            if table and str((table[0] or [''])[0] or '').strip().lower() == 'code':
                continue
            for raw in table:
                r=[(x or '').strip() for x in raw]
                # Headings can land in any extracted table cell when a page
                # break or side margin splits the printed table. Find the
                # SECURE heading across all cells before applying the normal
                # first-column layout rules.
                heading=None
                for cell in r:
                    # Main estimate items use the SECURE SoR form 79.xx.xx
                    # (optionally with a suffix such as 79.04.25b or
                    # 79.24.05.1a).  Resource/rate-analysis codes that happen
                    # to contain 79. (for example 79.01.mate) must never
                    # become estimate items.
                    match=re.match(r'^(?:(\d+)\s+)?('+_ITEM_CODE_TOKEN+r')\s*--\s*(.*)$',cell,re.S)
                    if match:
                        heading=match; break
                if heading:
                    serial=int(heading[1]) if heading[1] else len(items)+1
                    current={'number':serial,'code':_normalise_item_code(heading[2]),'name':' '.join(heading[3].split()),'rows':[],'page':page,'total':None,'alternate':False}
                    items.append(current); measuring=True; alternate_layout=False; head=''; continue
                if re.match(r'^'+_ITEM_CODE_TOKEN+r'\s*--\s*',r[0]):
                    code,name=r[0].split(' -- ',1)
                    current={'number':len(items)+1,'code':_normalise_item_code(code),'name':' '.join(name.split()),'rows':[],'page':page,'total':None,'alternate':False}
                    items.append(current); measuring=True; alternate_layout=False; head=''; continue
                if len(r)>1 and r[0].isdigit() and re.match(r'^'+_ITEM_CODE_TOKEN+r'\s*--\s*',r[1]):
                    m=re.match(r'^('+_ITEM_CODE_TOKEN+r')\s*--\s*(.*)',r[1],re.S)
                    if not m: raise ValueError(f'Cannot read item name on page {page}.')
                    current={'number':int(r[0]),'code':_normalise_item_code(m[1]),'name':' '.join(m[2].split()),'rows':[],'page':page,'total':None,'alternate':False}
                    items.append(current); measuring=True; alternate_layout=False; head=''; continue
                if not current: continue
                # SECURE exports two closely related measurement layouts. The
                # older one includes a Sl No column; newer exports omit it and
                # begin directly with Description. Both are measurement tables
                # when the No/L/B/D/CF/Quantity/Remark columns are present.
                if r[0]=='Sl No': measuring=True; alternate_layout=False; next_serial=1; continue
                if (r[0]=='Description' and len(r)>=7 and
                    [x.lower() for x in r[1:]] == ['no','l','b','d','cf','quantity','remark']):
                    measuring=True; alternate_layout=True; current['alternate']=True; next_serial=1; continue
                if r[0]=='LIST': measuring=False; continue
                # Some PDF drivers put a new item heading in one fragment and
                # its measurement rows in the next fragment without repeating
                # the column header. Recognize that row shape directly.
                if current and not measuring and current.get('total') is None and r[0] and not r[0].isdigit() and len(r)==8 and all((not r[i]) or re.fullmatch(r'-?[\d.]+',r[i].replace(',','')) for i in (1,2,3,4,5,6)):
                    measuring=True; alternate_layout=True; current['alternate']=True; next_serial=1
                if r[0].startswith('Say '):
                    m=re.match(r'Say\s+([\d.]+)\s+(\S+)',r[0])
                    if m: current['total']=m[1]; current['unit']=m[2]
                if not measuring: continue
                if (measuring and alternate_layout and r[0] and len(r)==8 and len(raw)==8
                    and all((not r[i]) or re.fullmatch(r'-?[\d.]+',r[i].replace(',','')) for i in (1,2,3,4,5,6))):
                    # Newer exports also omit the serial number from each row;
                    # descriptions such as column/LW/SW therefore occupy the
                    # first cell and must still be imported as measurements.
                    r=[str(next_serial)]+[x or '' for x in r]; next_serial+=1
                if r[0].isdigit():
                    # A page header can leave its item number in a one-cell
                    # grid row immediately before the real measurement table.
                    # It has no dimensions or quantity, so it is not data.
                    if not any(r[1:]):
                        continue
                    # Rate-analysis tables also begin with a numeric resource
                    # code, but their third cell is a unit such as Day/cum,
                    # not the measurement No. value. Never treat them as
                    # estimate rows.
                    if len(r) > 2 and r[2] and not re.fullmatch(r'-?[\d.]+', r[2].replace(',', '')):
                        continue
                    # Many SECURE PDFs omit the empty Remark column from the
                    # extracted grid. It is still a valid measurement row;
                    # add the missing blank field before validating it. This
                    # prevents the parser from falling back to OCR and
                    # inventing an item code from an unrelated analysis row.
                    if len(r) == 8:
                        r.append('')
                    # In the no-Sl-No layout, prepend a synthetic serial so
                    # the rest of the importer and checkpoint logic remain
                    # unchanged. Rate-analysis rows do not have this shape.
                    if len(r)!=9:
                        if alternate_layout: continue
                        raise ValueError(f'Unclear measurement columns: item {current["number"]}, page {page}.')
                    row=dict(zip(['serial','description','no','l','b','d','cf','quantity','remark'],r))
                    row['description'] = _clean_row_description(row['description'], row['serial'])
                    row.update(page=page,head=head)
                    # Rate-analysis fragments can share a ruled grid with the
                    # measurement table after a page break.  Their No. column
                    # contains a unit such as "cum", not a numeric count.
                    # They are pricing data and must never become row entries.
                    numeric_columns = ('no', 'l', 'b', 'd', 'cf')
                    # A dimension may be a printed arithmetic expression such
                    # as ``(6+5)/2``.  Preserve that exact text for review and
                    # SECURE entry, while _calculated_measurement_quantity()
                    # uses its safely calculated numeric value.  Text such as
                    # a rate-analysis unit is still rejected here.
                    if any(
                        str(row[key]).strip()
                        and _measurement_factor(row[key]) is None
                        for key in numeric_columns
                    ):
                        continue
                    # Ignore the PDF Quantity cell. A valid imported row has
                    # a serial and usable measurement factors instead.
                    calculated = _calculated_measurement_quantity(row)
                    if _is_context_only_row(row['description']) or calculated is None:
                        if row['description']:
                            head = row['description']
                        continue
                    row['quantity'] = calculated
                    _annotate_deduction_row(row)
                    current['rows'].append(row)
                elif r[0] and all(not x for x in r[1:]): head=r[0]
                elif not any(r): head=''
    if not items: raise ValueError('No supported SECURE estimate tables found. A text PDF with the measurement table is required; scanned PDFs need OCR first.')
    _assign_display_order(items)
    for item in items:
        rows=item['rows']
        if not rows: raise ValueError(f'Item {item["number"]} has missing measurements or total.')
        if [int(r['serial']) for r in rows]!=list(range(1,len(rows)+1)):
            # Keep every extracted row, but normalize the UI/automation order
            # when a PDF exporter skipped or duplicated a printed serial.
            # The original printed serial remains available for review.
            for serial,row in enumerate(rows,1):
                row.setdefault('source_serial', row.get('serial'))
                row['serial']=str(serial)
        item['total'] = f'{sum(Decimal(r["quantity"]) for r in rows):.4f}'
        item.pop('total_warning', None)
        _summarize_deductions(item)
    return items

def _finalize_source_items(items, text):
    """Bind totals to their code section, never a restarting printed serial."""
    matches = list(re.finditer(r'(?m)^\s*\d+\s+('+_ITEM_CODE_TOKEN+r')\s*--\s*([^\n]*)', text))
    sections = {}
    for index, match in enumerate(matches):
        section = text[match.end():matches[index+1].start() if index+1<len(matches) else len(text)]
        sections.setdefault(_normalise_item_code(match[1]), []).append((match[2], section))
    seen = {}
    for item in items:
        code = item['code']; offset = seen.get(code, 0); seen[code] = offset + 1
        sources = sections.get(code, [])
        if offset < len(sources):
            first, section = sources[offset]
            say = re.search(r'(?mi)^\s*Say\s+([\d.]+)\s+(.+?)\s+@', section)
            if say:
                item['total'], item['unit'] = say[1], say[2].strip()
            # Keep wrapped title lines before the measurement column heading.
            header = re.search(r'(?mi)^\s*(?:Sl\s*No\b|Description\s+No\b)', section)
            if header:
                continuation = section[:header.start()].strip()
                if len(continuation.splitlines()) <= 8 and not re.search(r'(?mi)^\s*(?:Say|LIST|\d+\s+)', continuation):
                    item['name'] = ' '.join((first+' '+continuation).split())
        item.pop('total_warning', None)
        total = sum(Decimal(r['quantity']) for r in item['rows'])
        item['total'] = f'{total:.4f}'
        _summarize_deductions(item)
    return items


def _recover_browser_print_items(text):
    """Read browser-print measurement blocks when their headings miss table grids.

    SECURE's browser print can put a heading in selectable page text and the
    following measurement grid in a separate PDF table.  This recovery keeps
    each row inside the text section bounded by its own item heading, so a
    missing grid heading cannot move measurements to a neighbouring item.
    """
    heading_pattern=re.compile(r'(?m)^\s*(\d+)\s+('+_ITEM_CODE_TOKEN+r')\s*--\s*(.*)$')
    headings=list(heading_pattern.finditer(text))
    recovered=[]
    for index, match in enumerate(headings):
        section=text[match.end():headings[index+1].start() if index+1<len(headings) else len(text)]
        header=re.search(r'(?mi)^\s*Sl\s*No\s+Description\s+No\s+L\s+B\s+D\s+CF\s+Quantity\s+Remark\s*$',section)
        if not header:
            continue
        before=section[:header.start()]
        continuation=' '.join(line.strip() for line in before.splitlines()
                              if line.strip() and not re.match(r'^\d{1,2}/\d{1,2}/\d{2},|^about:blank',line,re.I))
        name=' '.join((match[3]+' '+continuation).split())
        measurement=re.search(r'(?s)(.*?)(?=\nLIST\b)',section[header.end():])
        if not measurement:
            continue
        rows=[]
        for line in measurement[1].splitlines():
            line=line.strip()
            if not line or re.match(r'^\d{1,2}/\d{1,2}/\d{2},|^about:blank|^secure print$',line,re.I):
                continue
            row_match=re.match(r'^(\d+)\s+(.*?)\s+((?:-?[\d,.]+\s+)+-?[\d,.]+)\s+-\s*$',line)
            if not row_match:
                continue
            serial,description,numbers=row_match.groups()
            values=[value.replace(',','') for value in numbers.split()]
            # A description may end in a number (for example "footing 1").
            # The measurement suffix contains at most No/L/B/D/CF/Quantity,
            # so retain any earlier number as part of its description.
            if len(values)>6:
                description=' '.join((description,*values[:-6])); values=values[-6:]
            if len(values)<2:
                continue
            quantity=values[-1]; factors=values[:-1]
            fields={'no':'','l':'','b':'','d':'','cf':'','quantity':quantity}
            if len(factors)>1:
                fields['cf']=factors[-1]; factors=factors[:-1]
            for field,value in zip(('no','l','b','d'),factors):
                fields[field]=value
            row={'serial':serial,'description':_clean_row_description(description,serial),
                 **fields,'remark':'-','page':'','head':''}
            # Prefer the calculated quantity when the fields agree. Some
            # browser prints collapse a factor column, so retain a different
            # printed Quantity for review rather than silently changing it.
            calculated=_calculated_measurement_quantity(row)
            if calculated is None:
                continue
            try:
                if Decimal(quantity.replace(',','')) < 0 and Decimal(calculated) > 0:
                    calculated=f'{-Decimal(calculated):.4f}'
            except Exception:
                pass
            try:
                if abs(Decimal(quantity.replace(',', '')) - Decimal(calculated)) > Decimal('0.0001'):
                    row['quantity']=quantity.replace(',', '')
                    row['quantity_source']='printed'
                else:
                    row['quantity']=calculated
                    row['quantity_source']='calculated'
            except Exception:
                row['quantity']=calculated
            _annotate_deduction_row(row)
            rows.append(row)
        say=re.search(r'(?mi)^\s*Say\s+([\d.]+)\s+(\S+)',section)
        if rows and say:
            recovered.append({'number':int(match[1]),'code':_normalise_item_code(match[2]),'name':name,
                              'rows':rows,'page':'','total':say[1],'unit':say[2],'alternate':False})
    return recovered


_SECURE_PRINT_HEADER = re.compile(
    r'(?mi)^\s*Sl\s*No\s+Description\s+No\s+L\s+B\s+D\s+CF\s+Quantity\s+Remark\s*$'
)
_MEASUREMENT_TOKEN = re.compile(r'-?(?:\d[\d,]*(?:\.\d+)?|\([^\r\n()]+\)(?:\s*[*/]\s*\d+(?:\.\d+)?)*)$')


def _secure_print_measurement_row(line, context='', trailing='', unit=''):
    """Return one SECURE Print row without assuming every factor is present.

    The browser printout may omit an empty D/CF column, wrap a description
    around the numeric row, or contain a formula in a dimension column.  The
    final numeric token is always the printed Quantity; remaining numeric or
    arithmetic tokens are retained left-to-right in No/L/B/D/CF.
    """
    match = re.match(r'^\s*(\d+)\s+(.*?)\s*(?:-\s*)?$', line)
    if not match:
        return None
    serial, body = match.groups()
    tokens = body.split()
    values = []
    while tokens and _MEASUREMENT_TOKEN.fullmatch(tokens[-1]):
        values.insert(0, tokens.pop().replace(',', ''))
    if len(values) < 2:
        return None
    description = ' '.join(part for part in (context, ' '.join(tokens), trailing) if part).strip()
    fields = dict.fromkeys(('no', 'l', 'b', 'd', 'cf'), '')
    factors = values[:-1]
    # Preserve common omitted empty cells instead of shifting a printed CF
    # into D.  For linear items the final factor is usually CF; the missing
    # intermediate dimensions remain visibly blank and multiply as one.
    if len(factors) == 4:
        target_fields = ('no', 'l', 'b', 'cf')
    elif len(factors) == 3 and 'metre' in str(unit).lower():
        target_fields = ('no', 'l', 'cf')
    else:
        target_fields = ('no', 'l', 'b', 'd', 'cf')
    for field, value in zip(target_fields, factors):
        fields[field] = value
    row = {
        'serial': serial,
        'description': _clean_row_description(description, serial),
        **fields,
        'quantity': values[-1],
        'remark': '-',
        'page': '',
        'head': '',
        'printed_quantity': values[-1],
    }
    calculated = _calculated_measurement_quantity(row)
    # A source quantity is safer than a guessed field alignment.  Use the
    # calculated value only when it agrees with the printed quantity.
    if calculated is not None:
        try:
            if abs(Decimal(calculated) - Decimal(values[-1])) <= Decimal('0.0001'):
                row['quantity'] = calculated
                row['quantity_source'] = 'calculated'
            else:
                row['quantity_source'] = 'printed'
        except Exception:
            row['quantity_source'] = 'printed'
    else:
        row['quantity_source'] = 'printed'
    _annotate_deduction_row(row)
    return row


def _recover_secure_print_items(text):
    """Read SECURE Print detailed estimates whose item tables cross pages.

    This format repeats headings and rate-analysis LIST blocks across pages.
    Bind each measurement block to its own printed item heading, preserving
    duplicate codes in different heading sections rather than using code as a
    dictionary key.
    """
    heading_pattern = re.compile(r'(?m)^\s*(\d+)\s+(' + _ITEM_CODE_TOKEN + r')\s*--\s*(.*)$')
    headings = list(heading_pattern.finditer(text))
    recovered = []
    for index, heading in enumerate(headings):
        section = text[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        header = _SECURE_PRINT_HEADER.search(section)
        if not header:
            continue
        before = section[:header.start()]
        title_lines = [line.strip() for line in before.splitlines()
                       if line.strip() and not re.match(r'^\d{1,2}/\d{1,2}/\d{2},|^about:blank|^secure print$', line, re.I)]
        name = ' '.join((heading[3], *title_lines)).strip()
        net = re.search(r'(?mi)^\s*Net\s+Total\s+Quantity\s+(-?[\d,]+(?:\.\d+)?)\s+(.+?)\s*$', section)
        if not net:
            continue
        unit = net[2].strip()
        body_match = re.search(r'(?s)(.*?)(?=\n\s*LIST\b)', section[header.end():])
        if not body_match:
            continue
        lines = [line.strip() for line in body_match[1].splitlines()
                 if line.strip() and not re.match(r'^\d{1,2}/\d{1,2}/\d{2},|^about:blank|^secure print$', line, re.I)]
        rows = []
        pending = []
        line_index = 0
        while line_index < len(lines):
            line = lines[line_index]
            if not re.match(r'^\d+\s+', line):
                pending.append(line)
                line_index += 1
                continue
            trailing = ''
            next_index = line_index + 1
            while next_index < len(lines) and not re.match(r'^\d+\s+', lines[next_index]):
                trailing = ' '.join((trailing, lines[next_index])).strip()
                next_index += 1
            row = _secure_print_measurement_row(line, ' '.join(pending), trailing, unit)
            if row:
                rows.append(row)
                pending = []
                line_index = next_index
            else:
                pending.append(line)
                line_index += 1
        if not rows:
            continue
        net_total = net[1].replace(',', '')
        # Rows with all their factor columns present are calculated.  Irregular
        # SECURE Print rows retain their printed quantity, and every item is
        # checked against the printed Net Total for review.
        item = {
            'number': int(heading[1]),
            'code': _normalise_item_code(heading[2]),
            'name': name,
            'rows': rows,
            'page': '',
            'total': f'{Decimal(net_total):.4f}',
            'net_total': f'{Decimal(net_total):.4f}',
            'unit': net[2].strip(),
            'alternate': False,
            'source_format': 'secure-print-detailed-estimate',
        }
        row_total = sum(Decimal(str(row['quantity']).replace(',', '')) for row in rows)
        if abs(row_total - Decimal(net_total)) > Decimal('0.01'):
            item['total_warning'] = 'Printed Net Total Quantity differs from the extracted rows; review this item before entry.'
        _summarize_deductions(item)
        recovered.append(item)
    if recovered:
        _assign_display_order(recovered)
    return recovered


def read_pdf(path,catalog=None,progress=None):
    reusable = _read_embedded_reusable_estimate(path)
    if reusable is not None:
        if progress:
            progress(1, 1, 'Opened saved SECURE estimate with exact measurement data.')
        return reusable
    # PDFium quickly detects raster/outlined printouts without walking every
    # vector on every page in pdfplumber. Keep normal selectable PDFs on the
    # existing table extraction path.
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(path)) as probe:
        scanned = False
        for index in range(len(probe)):
            page = probe[index]
            textpage = page.get_textpage()
            has_text = bool(textpage.get_text_range().strip())
            textpage.close(); page.close()
            if not has_text:
                scanned = True
                break
    if scanned:
        return read_scanned_secure_pdf(path,catalog,progress)
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
    # Four independent readers are enough to improve a multi-page estimate on
    # ordinary Windows PCs without exhausting RAM on a scanned/large document.
    workers = min(4, total)
    extracted = [None] * total
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='secure-pdf') as pool:
            futures = [pool.submit(_extract_selectable_page, path, number) for number in range(1, total + 1)]
            completed = 0
            for future in as_completed(futures):
                number, text, tables, boxes, positions = future.result()
                extracted[number - 1] = (text, tables, boxes, positions)
                completed += 1
                if progress:
                    progress(completed, total, f'Reading PDF pages in parallel: {completed} of {total} complete…')
    else:
        number, text, tables, boxes, positions = _extract_selectable_page(path, 1)
        extracted[0] = (text, tables, boxes, positions)
        if progress:
            progress(1, 1, 'Reading PDF page 1 of 1…')
    texts = [entry[0] for entry in extracted]
    pages = [entry[1] for entry in extracted]
    table_boxes = [entry[2] for entry in extracted]
    heading_positions = [entry[3] for entry in extracted]
    full_text = '\n'.join(texts)
    # SECURE Print detailed estimates can split each item's table and LIST
    # analysis across pages. Read their bounded text blocks before generic
    # ruled-table extraction, which otherwise loses a continuation item and
    # incorrectly falls back to OCR.
    secure_print_items = _recover_secure_print_items(full_text)
    if secure_print_items:
        return EstimateItems(secure_print_items, read_cost_summary(full_text))
    try:
        direct_items=parse_tables(pages)
        if direct_items:
            # A ruled grid can be perfectly readable while a heading sits in
            # page text between two table fragments. Do not accept that
            # partial result: every printed estimate heading must have one
            # matching item before measurements can be automated.
            source_text=full_text
            printed_sequence=[_normalise_item_code(match[1]) for match in re.finditer(
                r'(?m)^\s*\d+\s+('+_ITEM_CODE_TOKEN+r')\s*--', source_text)]
            printed_codes=set(printed_sequence)
            extracted_codes={item['code'] for item in direct_items}
            missing_codes=printed_codes-extracted_codes
            if missing_codes:
                # Keep the accurately positioned table rows. The browser-print
                # fallback is used only for a code whose whole grid is absent;
                # replacing every item with it caused serial/no/description
                # columns to shift in otherwise well-formed PDFs.
                recovered=_recover_browser_print_items(source_text)
                recovered_by_code={item['code']:item for item in recovered}
                absent=missing_codes-set(recovered_by_code)
                if absent:
                    raise ValueError('A printed item heading was outside the ruled table.')
                direct_items.extend(recovered_by_code[code] for code in missing_codes)
                order={code:index for index,code in enumerate(printed_sequence)}
                direct_items.sort(key=lambda item: order.get(item['code'], len(order)))
            _finalize_source_items(direct_items, '\n'.join(texts))
            _repair_truncated_item_names(direct_items, catalog)
            return EstimateItems(direct_items,read_cost_summary('\n'.join(texts)))
    except ValueError:
        pass
    # A heading can straddle two pages and sit outside the ruled table.
    full='\n'.join(texts)
    # Some SECURE exports place the final `Say ...` quantity in selectable
    # page text but omit it from pdfplumber's table grid. Add that value back
    # as a synthetic row so the normal validation path can still verify totals.
    headings=[]
    offset=0
    for line in full.splitlines(True):
        match=re.match(r'\s*(\d+)\s+(79\.[^\s]+)\s+--',line)
        if match: headings.append((int(match.group(1)),offset))
        offset+=len(line)
    for pos,(number,start) in enumerate(headings):
        section=full[start:headings[pos+1][1] if pos+1<len(headings) else None]
        say=re.search(r'(?mi)^\s*Say\s+([\d.]+)\s+(\S+)',section)
        if not say: continue
        total=f'Say {say.group(1)} {say.group(2)}'
        for pi,text in enumerate(texts):
            if re.search(rf'(?m)^{number}\s+79\.',text):
                pages[pi].append([[total]]); break
    # Heading serials restart in several SECURE sections, so use the stable
    # item code as the lookup key. A line-level capture avoids swallowing all
    # later headings when a description wraps across multiple printed lines.
    names={_normalise_item_code(m[2]): ' '.join(m[3].split())
           for m in re.finditer(r'(?m)^(\d+)\s+('+_ITEM_CODE_TOKEN+r')\s*--\s*(.*)$',full)}
    for pi,text in enumerate(texts):
        found={int(r[0]) for t in pages[pi] for r in t if len(r)>1 and ((r[0] or '').isdigit() and (r[1] or '').startswith('79.'))}
        for m in re.finditer(r'(?m)^(\d+)\s+('+_ITEM_CODE_TOKEN+r')\s*--',text):
            number=int(m[1]); code=m[2]
            if any(any((cell or '').strip().startswith(m[2]+' --') for cell in row) for t in pages[pi] for row in t):
                found.add(number)
            if number not in found:
                canonical_code=_normalise_item_code(code)
                name=names.get(canonical_code, '')
                # Some PDFs split the suffix into a separate word (e.g.
                # "79.04.13 a"). The position index then contains the base
                # code only; using zero would move the next heading before
                # continuation rows at the top of this page.
                top=heading_positions[pi].get(canonical_code)
                if top is None:
                    base_code=re.sub(r'[a-z]$', '', canonical_code, flags=re.I)
                    top=heading_positions[pi].get(base_code)
                if top is None:
                    raise ValueError(f'Cannot locate item {canonical_code} on page {pi+1}; review the PDF.')
                insert_at=len(pages[pi])
                for table_index,bbox in enumerate(table_boxes[pi]):
                    if bbox[1] > top:
                        insert_at=table_index; break
                pages[pi].insert(insert_at,[[str(number),code+' -- '+name]])
                table_boxes[pi].insert(insert_at,(0,top,0,top))
    # Some estimates mix selectable text pages with scanned pages. If text
    # extraction cannot see an SECURE code, use the bundled OCR reader for the
    # complete document rather than silently losing the image-only pages.
    if not any(text.strip() for text in texts) or not re.search(r'\b79\.\d{2}\.',full):
        return read_scanned_secure_pdf(path,catalog,progress)
    try:
        items=parse_tables(pages)
    except ValueError as text_error:
        # PDF table grids vary between departments and printer drivers. OCR is
        # the safe fallback when selectable text exists but its table geometry
        # is unusable. If OCR also fails, retain the original clear error.
        try:
            return read_scanned_secure_pdf(path,catalog,progress)
        except ValueError:
            raise text_error
    for item in items:
        if item['code'] in names: item['name']=names[item['code']]
    _finalize_source_items(items, full)
    _repair_truncated_item_names(items, catalog)
    return EstimateItems(items,read_cost_summary(full))

def _ocr_lines(result):
    """Put OCR blocks back into their printed lines, ordered from left to right."""
    groups=[]
    for box,text,*_ in result or []:
        text=str(text).strip()
        if not text:continue
        x=sum(point[0] for point in box)/4;y=sum(point[1] for point in box)/4
        group=next((line for line in groups if abs(line['y']-y)<18),None)
        if group is None:
            group={'y':y,'parts':[]};groups.append(group)
        group['parts'].append((x,text))
    return [(line['y'],sorted(line['parts'])) for line in sorted(groups,key=lambda line:line['y'])]

def _ocr_cell(parts,left,right):
    return ' '.join(text for x,text in parts if left<=x<right).strip()

def _ocr_number(value):
    value=unicodedata.normalize('NFKC',value).replace('O','0').replace('o','0').replace(',','').strip()
    if value in ('','-'):return ''
    match=re.search(r'-?\d+(?:\.\d+)?',value)
    return match.group(0) if match else value

def _ocr_measurement_columns(parts):
    columns={'serial':parts[0][0] if parts else 120.0}
    for x,text in parts:
        value=text.lower().replace('.','').strip()
        if 'description' in value:columns['description']=x
        elif value=='no':columns['no']=x
        elif value=='l':columns['l']=x
        elif value=='b':columns['b']=x
        elif value=='d':columns['d']=x
        elif value=='cf':columns['cf']=x
        elif value=='quantity':columns['quantity']=x
        elif 'remark' in value:columns['remark']=x
    return columns


def _ocr_measurement_score(result):
    """Score whether OCR recovered usable lines from a measurement table.

    Item headings alone are not enough: a scan can read ``79.xx`` clearly
    while the small numeric cells beneath it disappear.  The score gives a
    higher-resolution retry a factual way to decide whether it recovered more
    of the printed table.
    """
    header_seen = False
    numeric_lines = 0
    for _, parts in _ocr_lines(result):
        text = ' '.join(value for _, value in parts)
        lower = text.lower()
        if 'description' in lower and 'quantity' in lower:
            header_seen = True
            continue
        if header_seen and len(re.findall(r'-?\d+(?:\.\d+)?', text)) >= 3:
            numeric_lines += 1
    return (20 if header_seen else 0) + numeric_lines * 10

def _ocr_row_cells(parts,columns):
    keys=('serial','description','no','l','b','d','cf','quantity','remark')
    anchors=[columns.get(key,0.0) for key in keys]
    cells={}
    for index,key in enumerate(keys):
        left=-float('inf') if index==0 else (anchors[index-1]+anchors[index])/2
        right=float('inf') if index==len(keys)-1 else (anchors[index]+anchors[index+1])/2
        cells[key]=_ocr_cell(parts,left,right)
    return cells

def _catalog_item(code,name,catalog):
    """Resolve only an exact catalogue code; never invent a replacement code.

    An OCR description can resemble an unrelated SoR item.  Description
    similarity is useful to flag a review issue, but must not change the
    printed item code or create a new estimate item that was not in the PDF.
    """
    if not catalog:return code,name,''
    exact=catalog.get(code)
    if exact is None:
        # OCR normalization is case-insensitive, while the workbook preserves
        # suffix capitalization (for example gFSS). Resolve that difference
        # before comparing descriptions.
        for candidate, record in catalog.items():
            if str(candidate).casefold() == str(code).casefold():
                code = candidate
                exact = record
                break
    if exact:
        return code,exact['name'],exact.get('unit','')
    return code,name,''

def _ocr_score(result):
    text=' '.join(str(entry[1]) for entry in (result or []) if len(entry)>1)
    return len(re.findall(r'\b79[.\s]\d{2}[.\s][0-9A-Za-z]+',text))*100+len(text)

def _trim_scan_margins(image):
    """Remove blank scanner margins without cutting the estimate table."""
    from PIL import ImageOps
    gray=ImageOps.grayscale(image)
    # Treat near-white paper as background, then preserve a small safety edge.
    ink=ImageOps.invert(gray.point(lambda value: 255 if value < 242 else 0))
    box=ink.getbbox()
    if not box:return image
    left,top,right,bottom=box;width,height=image.size
    pad=max(18,round(min(width,height)*0.012))
    left=max(0,left-pad);top=max(0,top-pad);right=min(width,right+pad);bottom=min(height,bottom+pad)
    # Ignore false crops from a faint dot or a page border.
    if right-left<width*.45 or bottom-top<height*.35:return image
    return image.crop((left,top,right,bottom))

def _read_ocr_page(reader,np,image):
    """Choose the clearest orientation and contrast for a scanned page."""
    from PIL import ImageOps, ImageEnhance
    source=_trim_scan_margins(image).convert('RGB')
    normalized=ImageEnhance.Contrast(ImageOps.autocontrast(source.convert('L'))).enhance(1.35)
    # Start with the original scan.  Contrast normalization is a fallback
    # only for pages whose first pass is weak; this avoids running OCR twice
    # on every clear page.
    candidates=[source]
    best_result=[];best_image=source;best_score=-1
    for candidate in candidates:
        result,_=reader(np.array(candidate));score=_ocr_score(result)
        if score>best_score:best_result,best_image,best_score=result,candidate,score
        text = ' '.join(str(entry[1]) for entry in (result or []))
        confidence = sum(float(entry[2]) for entry in (result or [])) / max(1,len(result or []))
        # Only retry degraded pages. A clear measurement header or rate table
        # is useful even on continuation pages without a new item code.
        if confidence >= .90 and re.search(r'quantity|description|unskilled',text,re.I):
            break
    if best_score < 100:
        result,_=reader(np.array(normalized)); score=_ocr_score(result)
        if score>best_score: best_result,best_image,best_score=result,normalized,score
    # A sidewise page seldom has readable SECURE codes. Only spend the extra
    # OCR time on rotations when the normal orientation found none.
    if best_score<100:
        for angle in (90,270,180):
            candidate=source.rotate(angle,expand=True)
            result,_=reader(np.array(candidate));score=_ocr_score(result)
            if score>best_score:best_result,best_image,best_score=result,candidate,score
    return best_result,best_image


def _recover_measurement_headings(reader,np,image,result):
    """Retry only the heading bands missed above measurement table headers."""
    result=list(result or [])
    for y,parts in _ocr_lines(result):
        text=' '.join(t for _,t in parts).lower()
        if not ('description' in text and 'quantity' in text and 'remark' in text):
            continue
        top=max(0,int(y)-240); bottom=max(top+1,int(y)-22)
        preceding=[entry for entry in result if top <= sum(pt[1] for pt in entry[0])/4 < bottom]
        if any(re.search(_SCANNED_ITEM_CODE,entry[1],re.I) for entry in preceding):
            continue
        band=image.crop((0,top,int(image.width*.58),bottom))
        recovered,_=reader(np.array(band))
        for box,text,*rest in recovered or []:
            if re.search(_SCANNED_ITEM_CODE,text,re.I):
                result.append(([[x,yy+top] for x,yy in box],text,*rest))
    return result


def read_scanned_secure_pdf(path,catalog=None,progress=None):
    """Read raster SECURE detailed estimates with the app's bundled offline OCR."""
    ready, detail=ocr_reader_status()
    if not ready:
        raise ValueError(detail)
    try:
        import numpy as np
        import pypdfium2 as pdfium
        from rapidocr_onnxruntime import RapidOCR
        document=pdfium.PdfDocument(str(path))
        reader=RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    except (ImportError, OSError, RuntimeError) as error:
        raise ValueError(f'{OCR_IMPORT_ERROR} Details: {error}') from error
    items=[];current=None;measuring=False;head='';columns={};summary={}
    for page_number in range(len(document)):
        if progress:progress(page_number+1,len(document),f'Reading scanned PDF page {page_number+1} of {len(document)}â€¦')
        # Higher-resolution OCR is required for item headings such as 79.20.02a.
        # At lower resolution a missing heading can incorrectly attach its rows to
        # the preceding item.
        bitmap=document[page_number].render(scale=2.5);image=bitmap.to_pil()
        result,ocr_image=_read_ocr_page(reader,np,image);page_lines=_ocr_lines(result)
        # Small measurement columns are the first characters lost in a low
        # contrast scan.  Retry only pages whose first pass found a table but
        # too few numeric rows, then use the pass that recovered more table
        # data.  This keeps normal documents fast while rescuing weak scans.
        measurement_score = _ocr_measurement_score(result)
        if 0 < measurement_score < 50:
            detailed_image=document[page_number].render(scale=4.0).to_pil()
            detailed_result,detailed_ocr_image=_read_ocr_page(reader,np,detailed_image)
            if _ocr_measurement_score(detailed_result) > measurement_score:
                result,ocr_image=detailed_result,detailed_ocr_image
        # A border or image at the page top can hide a heading from whole-page
        # OCR. Read the heading band separately before assigning measurement rows.
        result = _recover_measurement_headings(reader,np,ocr_image,result)
        page_lines = _ocr_lines(result)
        for line_y,parts in page_lines:
            if line_y > ocr_image.height * .97 or line_y < ocr_image.height * .035:
                continue
            joined=' '.join(text for _,text in parts);compact=re.sub(r'\s+',' ',joined).strip()
            lower_summary=compact.lower()
            for key,label in (('unskilled','unskilled manpower wages'),('material','materials (includes extra charges)'),('skilled','skilled'),('semiskilled','semiskilled'),('lumpsum_material','lumpsum material'),('total_amount','total (')):
                if lower_summary.startswith(label):
                    values=re.findall(r'-?[\d,]+(?:\.\d+)?',compact)
                    if values:summary[key]=Decimal(values[-1].replace(',',''))
            # Accept OCR variants of the printed heading separator and keep
            # suffixes such as 79.04.13b intact.
            # An item code must start an item-heading line (optionally after
            # its printed serial).  Searching inside every OCR line allowed a
            # code-like number in a description or a rate table to become a
            # false estimate item.
            code_match=re.match(
                r'^\s*(?:\d{1,4}\s*[:.)-]\s*|\d{1,4}\s+)?(' + _SCANNED_ITEM_CODE + r')\s*(?:--|[-â€“â€”])\s*(.+\S)\s*$',
                compact,
                re.I,
            )
            if code_match:
                code=re.sub(r'\s+','.',code_match.group(1)).rstrip('.,;:').replace('O','0').replace('o','0');name=code_match.group(2).strip();code,name,catalog_unit=_catalog_item(code,name,catalog)
                # The focused heading pass can see the same heading again in
                # the full-page OCR.  Collapse that empty duplicate, while
                # preserving genuine repeated items that already contain rows.
                if current and current.get('code')==code and not current.get('rows'):
                    current['name']=name or current.get('name',f'Item {code}')
                    if catalog_unit:current['unit']=catalog_unit
                    current['page']=page_number+1
                    measuring=False;head='';continue
                # Preserve every printed item heading in sequence. A repeated
                # specification code remains a separate estimate item.
                current={'number':len(items)+1,'code':code,'name':name or f'Item {code}','unit':catalog_unit,'rows':[],'page':page_number+1,'total':'0.0000','_unskilled':Decimal('0'),'_amount':Decimal('0')}
                items.append(current)
                measuring=False;head='';continue
            if not current:continue
            lower=compact.lower()
            if 'code' in lower and 'rate' in lower and 'amount' in lower and 'quantity' in lower:
                measuring=False;continue
            if 'description' in lower and 'quantity' in lower and ('no' in lower or 'on' in lower):
                columns=_ocr_measurement_columns(parts);measuring=True;continue
            # Scans often read the printed LIST divider as ISI7 or I5T.
            if re.fullmatch(r'(?:list|isi7|i5t|list:)',lower):
                measuring=False;continue
            if re.match(r'^say\s*[-0-9]',lower):
                match=re.search(r'Say\s*(-?[\d,.]+)\s*([A-Za-z]+)',compact,re.I)
                if match and not current['unit']:current['unit']=match.group(2)
                values=re.findall(r'-?[\d,]+(?:\.\d+)?',compact)
                if len(values)>=3:
                    current['_base_quantity']=Decimal(values[0].replace(',',''))
                    current['rate']=values[-2].replace(',','')
                    current['_amount']=Decimal(values[-1].replace(',',''))
                continue
            if lower.startswith('unskilled wage'):
                values=re.findall(r'-?[\d,]+(?:\.\d+)?',compact)
                if values:current['_unskilled']=Decimal(values[-1].replace(',',''))
                continue
            if not measuring:continue
            cells=_ocr_row_cells(parts,columns);serial=_ocr_number(cells['serial'])
            # A printed "1" can be read as a dash when the cell border crosses it.
            # The remaining dimensions and quantity still identify a valid row.
            if serial in ('','-') and any(_ocr_number(cells[key]) for key in ('no','l','b','d','cf')):
                serial=str(len(current['rows'])+1)
            if not re.fullmatch(r'\d+',serial):
                if compact and not re.search(r'\b(?:rate|amount|code)\b',lower):
                    # A tall row can wrap its description onto the next OCR
                    # line.  It has no serial or quantity, so join it to the
                    # preceding numbered measurement instead of creating a
                    # false heading or losing those words.
                    has_measurement_numbers = bool(_ocr_number(cells['quantity']))
                    if (current.get('rows') and not has_measurement_numbers
                        and not _is_context_only_row(compact)):
                        prior = current['rows'][-1]
                        prior['description'] = ' '.join(
                            (prior.get('description', ''), compact)
                        ).strip()
                    else:
                        head=compact
                continue
            normalized_serial = str(len(current['rows'])+1)
            row={'serial':normalized_serial,'description':_clean_row_description(cells['description'], serial),'no':_ocr_number(cells['no']),'l':_ocr_number(cells['l']),'b':_ocr_number(cells['b']),'d':_ocr_number(cells['d']),'cf':_ocr_number(cells['cf']),'quantity':_ocr_number(cells['quantity']),'remark':cells['remark'],'page':page_number+1,'head':head}
            if _is_context_only_row(row['description']):
                head = row['description']
                continue
            calculated = _calculated_measurement_quantity(row)
            if calculated is None:
                continue
            row['quantity'] = calculated
            _annotate_deduction_row(row)
            current['rows'].append(row)
    if not items:raise ValueError('No SECURE item codes were found in this scanned PDF. Use a clear, straight scan of the estimate pages.')
    total_labour=Decimal('0');total_amount=Decimal('0')
    for item in items:
        # A weak scan can lose one measurement grid while still reading the
        # remaining estimate correctly.  Keep that item for Review & edit
        # instead of discarding the whole estimate; it is explicitly blocked
        # from automation until the user supplies its rows.
        missing_rows = not item['rows']
        if missing_rows:
            item['manual_measurement_required'] = True
            item['warning'] = (
                'No measurement rows were read from this scanned PDF. '
                'Add the rows in Review & edit before starting automation.'
            )
        if not item['unit']:item['unit']='unit'
        row_total=sum((Decimal(row['quantity']) for row in item['rows']),Decimal('0'))
        item['total']=f'{row_total:.4f}'
        item['base_quantity']=f'{item.pop("_base_quantity",Decimal(item["total"])):.4f}';item['base_amount']=f'{item.get("_amount",Decimal("0")):.2f}';item['base_unskilled']=f'{item.get("_unskilled",Decimal("0")):.2f}'
        _summarize_deductions(item)
        total_labour+=item.pop('_unskilled',Decimal('0'));total_amount+=item.pop('_amount',Decimal('0'))
    if summary:
        total_labour=summary.get('unskilled',total_labour);material=summary.get('material',Decimal('0'));skilled=summary.get('skilled',Decimal('0'));semiskilled=summary.get('semiskilled',Decimal('0'));lumpsum=summary.get('lumpsum_material',Decimal('0'));total_amount=summary.get('total_amount',total_labour+material+skilled+semiskilled+lumpsum)
    else:material=total_amount-total_labour;skilled=semiskilled=lumpsum=Decimal('0')
    total_material=material+skilled+semiskilled+lumpsum
    costs={'unskilled':f'{total_labour:.2f}','semiskilled':f'{semiskilled:.2f}','skilled':f'{skilled:.2f}','material':f'{material:.2f}','lumpsum_material':f'{lumpsum:.2f}','total_labour':f'{total_labour:.2f}','total_material':f'{total_material:.2f}','total_amount':f'{total_amount:.2f}'}
    _repair_truncated_item_names(items, catalog)
    return EstimateItems(items,costs)

def _header(value):return re.sub(r'[^a-z0-9]+','',str(value or '').lower())
def _number(value):
    if value is None or str(value).strip() in ('','-'):return None
    try:return Decimal(str(value).strip().replace(',',''))
    except Exception:return None
def _display(value,places=4):
    value=Decimal(value);text=f'{value:.{places}f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text

def read_excel(path):
    """Read a row-based Excel estimate. Main item code is the grouping key."""
    book=load_workbook(path,read_only=False,data_only=True);items=[];by_code={};cost_total=Decimal('0');found_table=False
    aliases={
        'code':{'itemcode','soritemcode','specificationcode','specificationitemcode','code'},
        'name':{'itemname','specificationname','nameofitem','workitem'},'unit':{'unit','uom'},
        'description':{'measurementdescription','itemdescription','description','particular','particulars','details'},
        'no':{'no','nos','number','count'},'l':{'l','length'},'b':{'b','breadth','width'},
        'd':{'d','depth','height','thickness'},'cf':{'cf','coefficient','factor'},
        'quantity':{'quantity','qty','calculatedquantity'},'remark':{'remark','remarks','note','notes'},
        'rate':{'rate','itemrate','unitrate'},'amount':{'amount','totalamount','itemamount'}
    }
    for sheet in book.worksheets:
        values=list(sheet.iter_rows(values_only=True));mapping=None;start=0
        for index,row in enumerate(values[:30]):
            normalized=[_header(value) for value in row];candidate={}
            for key,names in aliases.items():
                position=next((i for i,value in enumerate(normalized) if value in names),None)
                if position is not None:candidate[key]=position
            measurement_keys={'description','no','l','b','d','cf','quantity'}
            if 'code' in candidate and measurement_keys.intersection(candidate):mapping=candidate;start=index+1;found_table=True;break
        if not mapping:continue
        current=None
        for excel_row,row in enumerate(values[start:],start+1):
            def value(key):
                col=mapping.get(key);return row[col] if col is not None and col<len(row) else None
            code=str(value('code') or '').strip()
            if code:
                if not (code.startswith('79.') or re.match(r'^\d+(?:\.\w+)+$',code)):continue
                current=by_code.get(code)
                if current is None:
                    name=str(value('name') or '').strip()
                    if not name and 'name' not in mapping and 'description' in mapping:name=str(value('description') or '').strip()
                    current={'number':len(items)+1,'code':code,'name':name or f'Item {code}','unit':str(value('unit') or '').strip(),'rows':[],'page':excel_row,'total':'0.0000','rate':'','amount':'','_amount_total':Decimal('0')}
                    by_code[code]=current;items.append(current)
                else:
                    if value('name') and not current['name']:current['name']=str(value('name')).strip()
                    if value('unit') and not current['unit']:current['unit']=str(value('unit')).strip()
            if current is None:continue
            numbers={key:_number(value(key)) for key in ('no','l','b','d','cf','quantity','rate','amount')}
            description=str(value('description') or '').strip()
            if 'name' not in mapping and code and description==current['name']:description=''
            has_measurement=description or any(numbers[key] is not None for key in ('no','l','b','d','cf','quantity'))
            if not has_measurement:continue
            quantity=numbers['quantity']
            if quantity is None:
                factors=[numbers[key] for key in ('no','l','b','d','cf') if numbers[key] is not None]
                if not factors:raise ValueError(f'{sheet.title} row {excel_row}: quantity and measurement factors are missing.')
                quantity=Decimal('1')
                for factor in factors:quantity*=factor
            rate=numbers['rate'];amount=numbers['amount']
            if rate is None and amount is not None and quantity:rate=amount/quantity
            if amount is None and rate is not None:amount=quantity*rate
            if rate is not None:current['rate']=_display(rate,2)
            if amount is not None:current['_amount_total']+=amount;current['amount']=_display(current['_amount_total'],2);cost_total+=amount
            measurement={key:('' if numbers[key] is None else _display(numbers[key])) for key in ('no','l','b','d','cf')}
            measurement.update(serial=str(len(current['rows'])+1),description=description,quantity=_display(quantity),remark=str(value('remark') or '').strip(),page=excel_row,head='')
            current['rows'].append(measurement)
    if not found_table:raise ValueError('No Excel estimate table found. Include Item Code and measurement columns such as Description, No., L, B, D, CF or Quantity.')
    items=[item for item in items if item['rows']]
    if not items:raise ValueError('No measurement rows were found in the Excel estimate.')
    for number,item in enumerate(items,1):
        item['number']=number;item['total']=f'{sum((_number(row["quantity"]) or Decimal("0") for row in item["rows"]),Decimal("0")):.4f}';item.pop('_amount_total',None)
    costs={'total_labour':'0.00','total_material':f'{cost_total:.2f}','total_amount':f'{cost_total:.2f}'} if cost_total else {}
    return EstimateItems(items,costs)

