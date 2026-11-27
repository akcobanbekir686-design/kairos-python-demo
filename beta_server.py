"""Separate Render entry point; no production sessions, database, or routes."""
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
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

# Disposable isolated verification. No public endpoint starts generation.
QA_RID = '9209244f-2306-4e67-b803-fbe2d639506f'
QA_DEADLINE = 1795765154
REPLAY_CERT = b"""-----BEGIN CERTIFICATE-----
MIIDHTCCAgWgAwIBAgIUXHhL1j8OCaAKwrZMs2EtDNCUhD8wDQYJKoZIhvcNAQEL
BQAwHjEcMBoGA1UEAwwTS0FJUk9TLXRlc3QtZml4dHVyZTAeFw0yNjExMjcwNjM5
MTRaFw0yNjExMjgwNjM5MTRaMB4xHDAaBgNVBAMME0tBSVJPUy10ZXN0LWZpeHR1
cmUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCc3HOJm3StKcix0Nf2
b1AFnQX5G4ihddN++XVgw4HaOwSP8mmkGfMYmSlIiGr7Rq585z8Ltc2Fvyfw9iS9
/t83N0AB6hRfkll35aLFbG92WMaw2xu9G0t5Qv3Vz06jj8otbJPPmKbkPmSb4ddp
Wv6LV0Eh64lDzMb7SCUnbFzgMxNWLCtgJWDbrvsoBkm8g+wv6KpxV4N73J/d2rzX
h0dB1hXlm37aEO6QWW0Vr9mGp8DrA6hrILkdKv7Ds4V7x5C/1zOSPR+VXPjEtjHK
Iyei4J8PckCjITa4jp9hfXL3wavWJh4Ggq7MF/dY2kXiAZXdy9I2Mn1PCZtublYo
N9n7AgMBAAGjUzBRMB0GA1UdDgQWBBSf0MISF/YHA/tK9gCpQfklj6iS9jAfBgNV
HSMEGDAWgBSf0MISF/YHA/tK9gCpQfklj6iS9jAPBgNVHRMBAf8EBTADAQH/MA0G
CSqGSIb3DQEBCwUAA4IBAQBUrBi12NvygzVClVfuWhdqF/9/tSxwi+xgPNm8dfFd
RB8e/8YVj1Zz2cvS2JmXFqG4BqjXVc9EmAPUNPlYJGVNtJDTk3NPRUPo804VEdV7
obd1tKqHepkSB8vGkDwR58I4UbE+EUY9KnyVwDF8yza7oIDpV2S+GqZizfJ7qFjh
S68Mrby/uJR4pnXRNzA1y7RuloXj/fMNkHfNoMIqGWHyuz17Fof3HdqCa+m/5OnC
rPza5A1Z5n40Cr/YEIvUVAvoVXkeSdvWBeWgelX9iPXyhZQvA/jfPJqsJAM1XYBq
XBO3MbbhRuAh8tge/9ZnVPxRHOwRDShFcYIcnnqLVs1a
-----END CERTIFICATE-----
"""

def preserve_failed_fixture(raw):
    # Only the synthetic, history-disabled fixture uses this callback.
    # A private replay can diagnose the same reply without a second paid call.
    import base64
    import subprocess
    import tempfile
    try:
        with tempfile.NamedTemporaryFile() as cert:
            cert.write(REPLAY_CERT)
            cert.flush()
            cipher = subprocess.run(['openssl','cms','-encrypt','-binary','-aes-256-cbc',
                                     '-outform','DER','-recip',cert.name], input=raw,
                                    capture_output=True, check=True, timeout=5).stdout
        encoded = base64.b64encode(cipher).decode('ascii')
        for pos in range(0,len(encoded),1200):
            print('KAIROS_FIXTURE_CIPHER',pos,encoded[pos:pos+1200],flush=True)
    except (OSError,subprocess.SubprocessError):
        print('KAIROS_FIXTURE_CIPHER unavailable',flush=True)

def verify_letter_once(ledger):
    import time
    from providers import JsonHTTPTransport
    cap = limits()
    if (os.getenv('RENDER_SERVICE_ID')!='srv-dc43rjijnfac73behcp0' or time.time()>=QA_DEADLINE
            or readiness()!='review_pause' or os.getenv('OPENAI_MODEL')!=MODEL or cap!=3_000_000_000):
        return
    owner = 5
    fixture = dict(id=QA_RID,text='Rüyamda kapısı aralık bir sınıfta ders anlatıyormuşum. Tebeşir elimdeymiş. Tahtaya ne yazacağımı hatırlamıyorum.',
                   symbols=['Kuş'],experience='rüya',share=True,history=False,remember=False)
    captured = []
    dispatched = 0
    def one_transport(payload,key,timeout):
        nonlocal dispatched
        if dispatched: raise ProviderError('already_submitted')
        dispatched += 1
        reply = JsonHTTPTransport()(payload,key,timeout)
        captured.append(reply)
        return reply
    report = {'model':MODEL}
    try:
        before = ledger.budget_summary(cap)
        try:
            receipt = ledger.receipt(owner,QA_RID)
        except ProviderError as error:
            if error.code!='unknown_request': raise
            if before['calls']!=4 or before['metered']!=13_804_000 or before['reserved']!=0:
                raise ProviderError('baseline_changed')
            provider = OpenAIResponsesProvider(model=MODEL,api_key=os.environ['OPENAI_API_KEY'],max_output_tokens=OUTPUT,
                                               timeout_seconds=55,enable_live=True,transport=one_transport)
            new = ledger.submit(owner,fixture,provider,cap)
            receipt = ledger.receipt(owner,QA_RID)
            if new!=receipt: raise ProviderError('receipt_mismatch')
        marks = receipt['console']['provenance']
        with ledger.transaction() as sql:
            history_count = sql('SELECT COUNT(*) FROM beta_history_v1 WHERE owner=? AND id=?',(owner,QA_RID)).fetchone()[0]
        report.update(state='done',words=len(receipt['letter'].split()),ordinal=receipt['ordinal'],history_rows=history_count,
                      no_early_seal=receipt['seal'] is None,source_separation=list(marks['mentioned'])==['Kapı'] and marks['tagged']==['Kuş'],
                      source_count=len(receipt['console']['sources']),inferences=len(marks['inferred']))
    except ProviderError as error:
        report['state'] = error.code
        if captured:
            # No response metadata, personal history, text or credential in plaintext logs.
            messages = [m for m in captured[0].get('output',[]) if m.get('type')=='message']
            preserve_failed_fixture(json.dumps(messages,ensure_ascii=False).encode())
    except Exception as error:
        report['state'] = type(error).__name__
    try:
        with ledger.transaction() as sql:
            row = sql('SELECT state,input_tokens,output_tokens,charge FROM beta_meter_v1 WHERE owner=? AND id=?',(owner,QA_RID)).fetchone()
        report.update(dispatched=dispatched,receipt=list(row) if row else None,budget=ledger.budget_summary(cap))
        print('KAIROS_LETTER_V3_QA '+json.dumps(report),flush=True)
    except Exception:
        print('KAIROS_LETTER_V3_QA ledger_unavailable',flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,body,mime='application/json; charset=utf-8'):
        raw=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(raw)
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
        server.ledger=ledger
        Thread(target=verify_letter_once,args=(ledger,),daemon=True).start()
        server.serve_forever()
