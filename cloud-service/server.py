"""Web framework API. Live portal execution is intentionally not enabled.

Deploy separately from GitHub Pages. Google identity and access are checked
against the existing licensing service for every protected request.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from domain import Jobs, calculate
from pdf_reader import read_upload, MAX_BYTES

ORIGIN = os.environ.get('WEB_ORIGIN','https://securemeasurementassistant.info')
LICENSE_URL = os.environ.get('LICENSE_URL','')
DATA = Path(os.environ.get('DATA_DIR','./data'))
JOBS = None

def access(token):
    if not token: raise PermissionError('Sign in with Google first.')
    if not LICENSE_URL.startswith('https://'): raise RuntimeError('Licensing service is not configured.')
    request = Request(LICENSE_URL.rstrip('/')+'/v1/license/start-task',
        data=json.dumps({'idToken':token}).encode(), headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=15) as response:
        state = json.load(response)
    if not state.get('userId') or not state.get('accessAllowed'):
        raise PermissionError('Active trial, subscription or task credit required.')
    return state

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # Do not log tokens, documents or portal input.
    def send(self,status,data):
        encoded = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if self.headers.get('Origin') == ORIGIN:
            self.send_header('Access-Control-Allow-Origin',ORIGIN)
            self.send_header('Vary','Origin')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers(); self.wfile.write(encoded)
    def do_OPTIONS(self):
        if self.headers.get('Origin') != ORIGIN: return self.send(403,{'error':'Origin denied.'})
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin',ORIGIN)
        self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.end_headers()
    def do_GET(self):
        if self.path == '/health':
            return self.send(200,{'status':'ok','version':'web-framework-1',
                'capabilities':{'valuation':True,'portalAutomation':False,'pdfReading':True,'scannedPdfReading':False},
                'licenseConfigured':LICENSE_URL.startswith('https://')})
        self.handle_api()
    def do_POST(self): self.handle_api()
    def handle_api(self):
        if self.headers.get('Origin') not in (None, ORIGIN): return self.send(403,{'error':'Origin denied.'})
        try:
            token = self.headers.get('Authorization','')
            if not token.startswith('Bearer '): raise PermissionError('Sign in required.')
            state = access(token[7:]); owner = state['userId']
            if self.path == '/v1/pdf/read' and self.command == 'POST':
                size = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/pdf' or not 0 < size <= MAX_BYTES:
                    raise ValueError('Choose a PDF no larger than 5 MB.')
                return self.send(200, read_upload(self.rfile.read(size)))
            payload = {}
            if self.command == 'POST':
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 1000000: raise ValueError('Request must be smaller than 1 MB.')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload,dict): raise ValueError('JSON object required.')
            route = urlparse(self.path).path
            if route == '/v1/access' and self.command == 'GET': return self.send(200,state)
            if route == '/v1/valuation' and self.command == 'POST': return self.send(200,calculate(payload))
            if route == '/v1/jobs' and self.command == 'POST':
                # No task credit is consumed until a worker verifies all portal saves.
                return self.send(503,{'error':'Hosted portal execution is awaiting deployment and live verification.'})
            if route.startswith('/v1/jobs/') and self.command == 'GET':
                return self.send(200,JOBS.get(owner,route.split('/')[-1]))
            return self.send(404,{'error':'Not found.'})
        except PermissionError as exc: self.send(403,{'error':str(exc)})
        except LookupError: self.send(404,{'error':'Not found.'})
        except (ValueError,SyntaxError,TypeError): self.send(400,{'error':'Invalid fields or arithmetic expression.'})
        except Exception: self.send(503,{'error':'Service unavailable. No portal entry was performed.'})

if __name__ == '__main__':
    DATA.mkdir(parents=True,exist_ok=True); JOBS = Jobs(DATA/'jobs.sqlite3')
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
