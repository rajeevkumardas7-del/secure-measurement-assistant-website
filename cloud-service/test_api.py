import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
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

    def test_missing_identity_rejected(self):
        self.assertEqual(self.request('POST', '/v1/valuation', {'rows': []})[0], 403)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.request('GET', '/v1/access', headers={'Origin': 'https://foreign.example'})[0], 403)

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


if __name__ == '__main__':
    unittest.main()
