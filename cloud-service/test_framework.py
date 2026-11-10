import tempfile
import unittest
from pathlib import Path
from domain import number, calculate, Jobs

class FrameworkTests(unittest.TestCase):
    def test_expression_and_empty(self):
        self.assertEqual(str(number('(6+5)/2')),'5.5')
        self.assertEqual(number('',blank='1'),1)
        self.assertEqual(number('7.000'),number('7'))
    def test_reject_code(self):
        for value in ['__import__("os")','2**10000','1/0','nan','True','[1]']:
            with self.assertRaises((ValueError,SyntaxError)): number(value)
    def test_totals_and_duplicate_codes(self):
        row={'code':'79.PWD.10.1','quantity':'(6+5)/2','rate':'100','labourRate':'20'}
        data=calculate({'rows':[row,row,{**row,'quantity':'-1'}],'personDays':'100','dailyWage':'300'})
        self.assertEqual(len(data['rows']),3)
        self.assertEqual(number(data['total']),1000)
        self.assertEqual(number(data['perPersonDay']),2)
        self.assertTrue(data['rows'][2]['deduction'])
        self.assertEqual(data['rows'][0]['quantity'],'(6+5)/2')
    def test_remaining_and_limit(self):
        with self.assertRaises(ValueError): calculate({'rows':[{'code':'x','quantity':3,'remaining':2}]})
        data=calculate({'rows':[{'code':'x','quantity':2,'labourRate':200}], 'personDays':1,'dailyWage':300})
        self.assertTrue(data['overLimit'])
    def test_tenant_ownership_and_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'jobs.sqlite3'; jobs=Jobs(path)
            job=jobs.create('alice','emb',{'rows':[]})
            with self.assertRaises(LookupError): jobs.get('bob',job['id'])
            with self.assertRaises(LookupError): jobs.cancel('bob',job['id'])
            self.assertEqual(Jobs(path).get('alice',job['id'])['state'],'awaiting_browser')
            self.assertEqual(jobs.cancel('alice',job['id'])['state'],'cancelled')

if __name__ == '__main__': unittest.main()
