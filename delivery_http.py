"""Bounded, non-streaming loopback delivery; DB writer fence covers socket write.
Cache is private process memory, never an authorization authority.
"""
from delivery_gate import deliver
import sqlite3
import threading
from urllib.parse import urlsplit, parse_qs
from secure_http import SecureHandler, SecureServer
from sessions import AuthorizedStore, Unauthorized
from core import Unavailable

class DeliveryHandler(SecureHandler):
    def do_GET(self):
        c=None; started=False; key=None
        self.connection.settimeout(2)
        try:
            q=urlsplit(self.path)
            args=parse_qs(q.query)
            if q.path!='/content' or set(args)!={'id'} or len(args['id'])!=1:
                self.reply(400,{'error':'invalid_request'}); return
            auth=self.headers.get('Authorization','')
            if not auth.startswith('Bearer ') or not 1<=len(auth[7:])<=512 or any(x.isspace() for x in auth[7:]):
                raise Unauthorized()
            c=AuthorizedStore(self.server.db_path,auth[7:]); uid=args['id'][0]
            key=(c.request_owner,uid)
            with c.atomic():
                current=c._read(c.request_owner,uid)
                with self.server.cache_lock:
                    prepared=self.server.cache.get(key)
                    if prepared is None:
                        prepared=current
                        if len(prepared.encode())>65536: raise ValueError()
                        if len(self.server.cache)>=128: self.server.cache.clear()
                        self.server.cache[key]=prepared
            self.server.after_prepare()
            # No await, queue or lock release between final check and write.
            def build():
                current=c._read(c.request_owner,uid)
                permission=c._permissions(c.request_owner,uid)
                if current!=prepared: raise Unavailable('changed')
                return {'text':prepared,'reason':permission['reason']}
            deliver(self,c,build)
        except (Unauthorized,Unavailable) as exc:
            if key:
                with self.server.cache_lock: self.server.cache.pop(key,None)
            if not started: self.reply(401 if isinstance(exc,Unauthorized) else 409,{'error':'content_unavailable'})
        except (ValueError,sqlite3.Error):
            if not started: self.reply(503,{'error':'delivery_failed'})
        except OSError: pass
        finally:
            if c: c.close()

class DeliveryServer(SecureServer):
    def __init__(self,path):
        super().__init__(path)
        self.RequestHandlerClass=DeliveryHandler
        self.cache={}; self.cache_lock=threading.Lock()
    def after_prepare(self): pass
