import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch
import server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def request(self, method, path, data=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_health_does_not_claim_automation(self):
        status, body = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertFalse(body['capabilities']['portalAutomation'])
        self.assertIsInstance(body['capabilities']['trainedPdfReader'], bool)
        self.assertIsInstance(body['capabilities']['scannedPdfReading'], bool)

    def test_missing_identity_rejected(self):
        self.assertEqual(self.request('POST', '/v1/valuation', {'rows': []})[0], 403)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.request('GET', '/v1/access', headers={'Origin': 'https://foreign.example'})[0], 403)

    def test_pdf_preflight_allows_reader_mode_header(self):
        connection = HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            connection.request('OPTIONS', '/v1/pdf/read', headers={
                'Origin': server.ORIGIN,
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'authorization, content-type, x-reader-mode',
            })
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 204)
            self.assertIn('X-Reader-Mode', response.getheader('Access-Control-Allow-Headers', ''))
        finally:
            connection.close()

    @patch.object(server, 'LICENSE_URL', 'https://license.example')
    @patch('server.urlopen')
    def test_access_returns_expired_state_for_payment_flow(self, urlopen):
        urlopen.return_value = BytesIO(json.dumps({
            'userId': 'google.alice', 'accessAllowed': False,
            'access': 'subscription_required', 'trialSecondsRemaining': 0,
        }).encode())
        state = server.access('expired-id-token')
        self.assertFalse(state['accessAllowed'])
        self.assertEqual(state['access'], 'subscription_required')

    @patch('server.access', side_effect=PermissionError('Expired'))
    def test_expired_access_rejected(self, access):
        self.assertEqual(self.request('GET', '/v1/access', headers={'Authorization': 'Bearer sample'})[0], 403)

    @patch('server.access', return_value={'userId': 'alice', 'accessAllowed': True})
    def test_calculation_and_disabled_execution(self, access):
        headers = {'Authorization': 'Bearer sample'}
        status, body = self.request('POST', '/v1/valuation', {'rows': [{'code': 'x', 'quantity': '(6+5)/2', 'rate': '10'}]}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(float(body['total']), 55)
        self.assertEqual(self.request('POST', '/v1/jobs', {'rows': []}, headers)[0], 503)
        self.assertEqual(self.request('POST', '/v1/valuation', {'rows': [{'code': 'x', 'quantity': '__import__(1)'}]}, headers)[0], 400)

    @patch('server.access', return_value={'userId': 'alice', 'accessAllowed': True})
    def test_measurement_calculation_keeps_expression_and_deduction(self, access):
        headers = {'Authorization': 'Bearer sample'}
        row = {'serial':'2','no':'1','l':'(6+5)/2','b':'2','d':'1','cf':'1','quantity':'-11','head':'deduction'}
        status, body = self.request('POST', '/v1/measurements/calculate', {'rows':[row]}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(body['rows'][0]['quantity'], '-11.0000')
        self.assertEqual(body['rows'][0]['entry_section'], 'deduction')

    @patch('server.access', return_value={'userId': 'alice', 'accessAllowed': False, 'access':'subscription_required'})
    def test_expired_access_can_check_state_but_cannot_calculate(self, access):
        headers = {'Authorization': 'Bearer sample'}
        self.assertEqual(self.request('GET', '/v1/access', headers=headers)[0], 200)
        self.assertEqual(self.request('POST', '/v1/valuation', {'rows': []}, headers)[0], 402)

    @patch('server.access', return_value={'userId': 'alice', 'accessAllowed': False, 'access':'subscription_required'})
    def test_payment_qr_only_accepts_official_payu_checkout(self, access):
        headers = {'Authorization': 'Bearer sample'}
        status, body = self.request('POST', '/v1/payment/qr', {'url':'https://u.payu.in/PAYUMN/example'}, headers)
        self.assertEqual(status, 200)
        self.assertTrue(body['image'].startswith('data:image/svg+xml;base64,'))
        self.assertEqual(self.request('POST', '/v1/payment/qr', {'url':'https://payu.in.attacker.example/x'}, headers)[0], 400)


if __name__ == '__main__':
    unittest.main()
