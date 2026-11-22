import io
import unittest
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from pdf_reader import read_upload
from estimate import _secure_print_measurement_row, ocr_reader_status
import pdf_reader

def fixture(lines):
    writer=PdfWriter(); page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    safe=[line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)') for line in lines]
    stream.set_data(('BT /F1 10 Tf 30 750 Td 14 TL '+' '.join('('+line+') Tj T*' for line in safe)+' ET').encode())
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=io.BytesIO();writer.write(output);return output.getvalue()

class PdfTests(unittest.TestCase):
    def test_ocr_band_overlap_keeps_each_line_once(self):
        from types import SimpleNamespace
        from estimate import _read_ocr_bands
        image = SimpleNamespace(width=100, height=600, crop=lambda bounds: bounds)
        def reader(bounds):
            top, bottom = bounds[1], bounds[3]
            return [([[0,y-top-5],[90,y-top-5],[90,y-top+5],[0,y-top+5]], str(y), .99)
                    for y in (250,256,512) if top < y < bottom], None
        rows = _read_ocr_bands(reader, SimpleNamespace(array=lambda value: value), image)
        self.assertEqual([row[1] for row in rows], ['250','256','512'])
        self.assertEqual([sum(p[1] for p in row[0])/4 for row in rows], [250,256,512])

    def test_image_only_pdf_reads_item_and_measurement(self):
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        writer = PdfWriter()
        writer.add_page(PdfReader(io.BytesIO(fixture([]))).pages[0])
        stream = DecodedStreamObject()
        text = ['BT /F1 14 Tf 30 720 Td (1: 79.03.01c -- Excavation in soil) Tj ET']
        for y, cells in [(670, ['Sl No', 'Description', 'No', 'L', 'B', 'D', 'CF', 'Quantity', 'Remark']),
                         (630, ['1', 'Footing', '2', '5', '3', '1', '1', '30', '-'])]:
            for x, value in zip([30, 85, 220, 265, 310, 355, 400, 450, 530], cells):
                text.append(f'BT /F1 12 Tf {x} {y} Td ({value}) Tj ET')
        stream.set_data('\n'.join(text).encode())
        writer.pages[0][NameObject('/Contents')] = writer._add_object(stream)
        source = io.BytesIO(); writer.write(source)
        with pdfium.PdfDocument(source.getvalue()) as document:
            image = document[0].render(scale=3).to_pil().convert('RGB')
            raster = io.BytesIO(); image.save(raster, format='PDF', resolution=216)
        result = read_upload(raster.getvalue())
        self.assertEqual(result['items'][0]['code'], '79.03.01c')
        self.assertEqual(result['items'][0]['rows'][0]['quantity'], '30.0000')

    def test_reader_memory_limit_releases_slot(self):
        with patch('pdf_reader.reader_memory_kb', return_value=pdf_reader.MAX_READER_KB + 1):
            with self.assertRaisesRegex(ValueError, 'memory limit'):
                read_upload(fixture([]))
        self.assertTrue(pdf_reader.SLOT.acquire(blocking=False))
        pdf_reader.SLOT.release()

    def test_pdf_larger_than_five_mb(self):
        raw = fixture(['1 79.03.01c -- Excavation',
            'Sl No Description No L B D CF Quantity Remark',
            '1 footing 2 5 3 1 1 30 -', 'Net Total Quantity 30 cum', 'LIST'])
        result = read_upload(raw + b'\n%' + b' ' * (6 * 1024 * 1024))
        self.assertEqual(result['items'][0]['code'], '79.03.01c')

    def test_invalid_file(self):
        with self.assertRaises(ValueError):read_upload(b'not a PDF')
    def test_blank_pdf_reports_no_items_after_reader_path(self):
        ready, _ = ocr_reader_status()
        message = 'No SECURE item codes' if ready else 'scanned-PDF reader is unavailable'
        with self.assertRaisesRegex(ValueError,message):read_upload(fixture([]))
    def test_formula_and_blank_cells(self):
        row=_secure_print_measurement_row('1 formula 2 (6+5)/2 3 33.0000 -')
        self.assertEqual(row['l'],'(6+5)/2')
        self.assertEqual(row['d'],'')
    def test_real_pdf_worker(self):
        lines=['1 79.03.01c -- Excavation','Sl No Description No L B D CF Quantity Remark',
            '1 footing 2 5 3 1 1 30 -','Net Total Quantity 30 cum','LIST',
            '1 79.03.01c -- Second excavation','Sl No Description No L B D CF Quantity Remark',
            '1 formula 2 (6+5)/2 3 33 -','Net Total Quantity 33 cum','LIST']
        result=read_upload(fixture(lines))
        self.assertEqual(len(result['items']),2)
        self.assertEqual(result['items'][0]['code'],result['items'][1]['code'])
        self.assertEqual(result['items'][1]['rows'][0]['l'],'(6+5)/2')
        self.assertTrue(result['reviewRequired'])

if __name__=='__main__':unittest.main()
