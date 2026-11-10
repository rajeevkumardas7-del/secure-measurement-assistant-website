import io
import unittest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from pdf_reader import read_upload
from estimate import _secure_print_measurement_row

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
    def test_invalid_file(self):
        with self.assertRaises(ValueError):read_upload(b'not a PDF')
    def test_blank_pdf_requires_ocr(self):
        with self.assertRaisesRegex(ValueError,'OCR'):read_upload(fixture([]))
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
