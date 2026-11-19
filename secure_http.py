"""Authenticated loopback reference. Separate from legacy fixed-user test server.
No login endpoint, TLS, refresh, cache, UI or production deployment.
"""
from delivery_gate import deliver, beginning_body
import json
import sqlite3
from http.server import ThreadingHTTPServer
from local_http import Handler
from sessions import AuthorizedStore, Unauthorized
from core import Conflict, Unavailable

class SecureHandler(Handler):
    def do_POST(self):
        c=None
        try:
            auth=self.headers.get('Authorization','')
            if not auth.startswith('Bearer ') or not 1<=len(auth[7:])<=512 or any(x.isspace() for x in auth[7:]):
                raise Unauthorized()
            c=AuthorizedStore(self.server.db_path,auth[7:])
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384: raise ValueError('size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise ValueError('body')
            routes={
                '/source/user':('request_id','text'),
                '/source/copy':('request_id','source_id'),
                '/source/derive':('derivation_id','derived_from'),
                '/araf/transfer':('transition_id','expected_version','request_id'),
                '/b1':('transition_id','axiom_id','trace_source','intent_source','expected_araf_version'),
                '/account/erase':(),
            }
            if self.path not in routes:
                self.reply(404,{'error':'not_found'}); return
            allowed=set(routes[self.path])
            if self.path=='/source/derive':
                if 'field_origin' in data and not isinstance(data['field_origin'],str): raise ValueError('origin')
                data={k:v for k,v in data.items() if k!='field_origin'}
            if set(data)!=allowed: raise ValueError('fields')
            self.server.before_action(self.path)
            owner=c.request_owner
            if self.path=='/account/erase':
                c.erase_owner(owner)
                status,result,replay=200,{'status':'active_store_erased'},None
            elif self.path=='/b1':
                for k in ('transition_id','axiom_id','trace_source'):
                    if not isinstance(data[k],str) or not data[k]: raise ValueError('identity')
                if data['intent_source'] is not None and not isinstance(data['intent_source'],str): raise ValueError('intent')
                receipt=c.begin(owner,data['transition_id'],data['axiom_id'],data['trace_source'],
                                data['intent_source'],expected_araf_version=data['expected_araf_version'],with_receipt=True)
                replay=receipt['replay']; status=200 if replay else 201
                self.server.before_delivery(self.path)
                deliver(self,c,lambda: beginning_body(c,owner,data['transition_id']),status,replay)
                return
            else:
                method={'/source/user':c.user_submission,'/source/copy':c.copy_source,
                        '/source/derive':c.consume_derivation,'/araf/transfer':c.transfer_araf}[self.path]
                replay,result=method(owner,*(data[k] for k in routes[self.path]))
                status=200 if replay else 201
        except Unauthorized:
            self.reply(401,{'error':'unauthorized'}); return
        except Conflict as exc:
            self.reply(409,{'error':exc.code}); return
        except Unavailable:
            self.reply(409,{'error':'source_unavailable'}); return
        except (ValueError,TypeError):
            self.reply(400,{'error':'invalid_request'}); return
        except sqlite3.Error:
            self.reply(503,{'error':'transaction_failed'}); return
        finally:
            if c is not None: c.close()
        self.reply(status,result,replay)

class SecureServer(ThreadingHTTPServer):
    daemon_threads=False
    block_on_close=True
    def __init__(self,path):
        self.db_path=path
        super().__init__(('127.0.0.1',0),SecureHandler)
    def before_action(self,path): pass
    def before_delivery(self,path): pass
