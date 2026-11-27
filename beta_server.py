"""Separate Render entry point; no production sessions, database, or routes."""
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from beta_reading import MODEL, OUTPUT, prices
from beta_store import Ledger
from providers import OpenAIResponsesProvider, ProviderError

def limits():
    value=os.getenv('KAIROS_APPROVED_CENTS','0')
    return int(value)*10_000_000 if value.isascii() and value.isdecimal() and 0<int(value)<=300 else 0

def readiness():
    if not os.getenv('OPENAI_API_KEY','').strip(): return 'missing_key'
    if not limits(): return 'budget_closed'
    try: prices(os.getenv('OPENAI_MODEL',MODEL))
    except ProviderError: return 'model_price_unverified'
    if not os.getenv('KAIROS_TEST_DB'): return 'storage_unavailable'
    return 'configured' if os.getenv('KAIROS_ALLOW_V2_GENERATION')=='1' else 'review_pause'

def db_connect():
    import psycopg
    return psycopg.connect(os.environ['KAIROS_TEST_DB'],connect_timeout=5)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,body,mime='application/json; charset=utf-8'):
        raw=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        delivered = True
        try:
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            delivered = False
        finally:
            if hasattr(self,'request_started'):
                state = body.get('error','ok') if isinstance(body,dict) else 'ok'
                elapsed = round((time.monotonic()-self.request_started)*1000)
                print('KAIROS_HTTP',self.path,status,state,elapsed,'ms',
                      'sent' if delivered else 'peer_closed',flush=True)
    def do_GET(self):
        files={'/':('beta.html','text/html; charset=utf-8'),'/beta.js':('beta.js','text/javascript; charset=utf-8')}
        path=urlsplit(self.path).path
        if path in files:
            f,mime=files[path];return self.send(200,Path(__file__).with_name(f).read_bytes(),mime)
        if path=='/healthz': return self.send(200,{'status':'ok','stage':'closed-beta'})
        if path=='/status':
            with self.server.audit_lock:
                if not self.server.budget_logged and os.getenv('KAIROS_TEST_DB'):
                    try:
                        summary=self.server.ledger.budget_summary(limits())
                        print('KAIROS_BETA_BUDGET '+json.dumps(summary),flush=True)
                        self.server.budget_logged=True
                    except Exception:
                        return self.send(503,{'state':'storage_unavailable'})
            return self.send(200,{'state':readiness(),'model':os.getenv('OPENAI_MODEL',MODEL)})
        self.send(404,{})
    def do_POST(self):
        if self.path not in ('/reading','/receipt','/receipts'): return self.send(404,{})
        self.request_started = time.monotonic()
        print('KAIROS_HTTP',self.path,'received',flush=True)
        try:
            origin=self.headers.get('Origin','')
            if not origin or origin!='https://'+self.headers.get('Host',''): raise ProviderError('origin',403)
            if self.headers.get_content_type()!='application/json': raise ValueError()
            size=int(self.headers.get('Content-Length','0'))
            if not 1<=size<=18000: raise ValueError()
            offered=self.headers.get('Authorization','')
            owner=None
            for i in range(1,6):
                key=os.getenv('BETA_PASS_'+str(i),'')
                if len(key)>=24 and hmac.compare_digest(offered.encode(),('Bearer '+key).encode()): owner=i
            if owner is None: raise ProviderError('beta_only',403)
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict): raise ValueError()
            if self.path=='/receipts':
                return self.send(200,{'items':self.server.ledger.receipts(owner)})
            if self.path=='/receipt':
                rid=body.get('id')
                if not isinstance(rid,str) or len(rid)!=36: raise ValueError()
                return self.send(200,self.server.ledger.receipt(owner,rid))
            if body.get('share') is not True: raise ProviderError('consent_required',403)
            ready=readiness()
            if ready not in ('configured','review_pause'): raise ProviderError(ready,503)
            provider=OpenAIResponsesProvider(model=os.getenv('OPENAI_MODEL',MODEL),api_key=os.environ['OPENAI_API_KEY'],
                                            max_output_tokens=OUTPUT,timeout_seconds=55,enable_live=ready=='configured')
            result=self.server.ledger.submit(owner,body,provider,limits(),paused=ready!='configured')
        except ProviderError as e: return self.send(e.status,{'error':e.code})
        except (TypeError,ValueError,AttributeError): return self.send(400,{'error':'invalid_request'})
        except Exception: return self.send(503,{'error':'storage_unavailable'})
        self.send(200,result)

if __name__=='__main__':
    ledger=Ledger(db_connect)
    # Do not reset missing accounting tables after activation.
    if not limits() and os.getenv('KAIROS_TEST_DB'): ledger.initialize()
    with ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','10000'))),Handler) as server:
        server.audit_lock=Lock()
        server.budget_logged=False
        server.ledger=ledger;server.serve_forever()
