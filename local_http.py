"""Loopback-only reference transport. No authentication or production deployment.

Test identity is server-owned. Fault injection subclass lives in test_http_retry.py.
POST /b1 and provenance ingress; no debug endpoint or environment fault flag.
"""
from delivery_gate import deliver, beginning_body
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from core import Core, Conflict, Unavailable
from origin import Origin

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,status,body,replay=None):
        raw=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        if replay is not None: self.send_header('X-Idempotent-Replay','true' if replay else 'false')
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        if self.path in ('/source/user','/source/derive','/source/copy','/araf/transfer'):
            self.source_post(); return
        if self.path!='/b1':
            self.reply(404,{'error':'not_found'}); return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384: raise ValueError('body_size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict) or set(data)!={
                'transition_id','axiom_id','trace_source','intent_source','expected_araf_version'}:
                raise ValueError('fields')
            for k in ('transition_id','axiom_id','trace_source'):
                if not isinstance(data[k],str) or not data[k]: raise ValueError('identity')
            if data['intent_source'] is not None and not isinstance(data['intent_source'],str):
                raise ValueError('intent')
            c=Core(self.server.db_path)
            try:
                receipt=c.begin('test-user',data['transition_id'],data['axiom_id'],data['trace_source'],
                    data['intent_source'],expected_araf_version=data['expected_araf_version'],with_receipt=True)
            finally: c.close()
        except Conflict as exc:
            self.reply(409,{'error':exc.code}); return
        except Unavailable:
            self.reply(409,{'error':'source_unavailable'}); return
        except (ValueError,TypeError):
            self.reply(400,{'error':'invalid_request'}); return
        # Core.begin has exited its atomic context and committed before this hook.
        # No send_response/end_headers/wfile.write has run on the success path yet.
        if self.server.after_commit(self,receipt): return
        c=Core(self.server.db_path)
        try:
            deliver(self,c,lambda: beginning_body(c,'test-user',data['transition_id']),
                    200 if receipt['replay'] else 201,receipt['replay'])
        except Unavailable:
            self.reply(409,{'error':'source_unavailable'})
        finally: c.close()

    def source_post(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384: raise ValueError('body_size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError('fields')
            if self.path=='/source/user':
                if set(data)!={'request_id','text'}: raise ValueError('fields')
            elif self.path=='/source/copy':
                if set(data)!={'request_id','source_id'}: raise ValueError('fields')
            elif self.path=='/araf/transfer':
                if set(data)!={'request_id','transition_id','expected_version'}: raise ValueError('fields')
            else:
                if not {'derivation_id','derived_from'}<=set(data) or set(data)-{'derivation_id','derived_from','field_origin'}:
                    raise ValueError('fields')
                if 'field_origin' in data and not isinstance(data['field_origin'],str): raise ValueError('origin')
            c=Origin(self.server.db_path)
            try:
                if self.path=='/source/user':
                    replay,result=c.user_submission('test-user',data['request_id'],data['text'])
                elif self.path=='/source/copy':
                    replay,result=c.copy_source('test-user',data['request_id'],data['source_id'])
                elif self.path=='/araf/transfer':
                    replay,result=c.transfer_araf('test-user',data['transition_id'],data['expected_version'],data['request_id'])
                else:
                    replay,result=c.consume_derivation('test-user',data['derivation_id'],data['derived_from'])
            finally: c.close()
        except Conflict as exc:
            self.reply(409,{'error':exc.code}); return
        except Unavailable:
            self.reply(409,{'error':'source_unavailable'}); return
        except (ValueError,TypeError):
            self.reply(400,{'error':'invalid_request'}); return
        self.reply(200 if replay else 201,result,replay)

class LocalServer(HTTPServer):
    def __init__(self,db_path):
        self.db_path=db_path
        super().__init__(('127.0.0.1',0),Handler)
    def after_commit(self,handler,receipt): return False
