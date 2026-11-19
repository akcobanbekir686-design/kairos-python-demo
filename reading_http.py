"""Authenticated LOOPBACK transport; demo by default, trusted provider injection.
No login or public deployment. WebServer remains demo-only.
"""
import json
import sqlite3
from urllib.parse import urlsplit, unquote
from reading_flow import NODES
from recovery_flow import RecoveryFlow as ReadingFlow
from sessions import Unauthorized
from core import Conflict, Unavailable
from delivery_http import DeliveryHandler, DeliveryServer
from delivery_gate import deliver
from providers import DemoProvider, ProviderError

class ReadingHandler(DeliveryHandler):
    def do_POST(self):
        if self.path.startswith('/flow/'): self.flow('POST')
        else: super().do_POST()
    def do_GET(self):
        if self.path.startswith('/flow/'): self.flow('GET')
        else: super().do_GET()
    def do_DELETE(self):
        if self.path.startswith('/flow/'): self.flow('DELETE')
        else: self.reply(404,{'error':'not_found'})
    def body(self,required,optional=()):
        if self.headers.get('Transfer-Encoding'): raise ValueError('encoding')
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=16384: raise ValueError('size')
        data=json.loads(self.rfile.read(length))
        if not isinstance(data,dict) or not set(required)<=set(data) or set(data)-set(required)-set(optional):
            raise ValueError('fields')
        return data
    def flow(self,method):
        f=None
        self.connection.settimeout(2)
        try:
            auth=self.headers.get('Authorization','')
            token=auth[7:] if auth.startswith('Bearer ') else ''
            if not 1<=len(token)<=512 or any(x.isspace() for x in token): raise Unauthorized()
            f=ReadingFlow(self.server.db_path,token)
            path=urlsplit(self.path)
            if path.query or path.fragment: raise ValueError('query')
            route=path.path
            required_method=None
            if route in ('/flow/append','/flow/next','/flow/respond','/flow/close','/flow/recover','/flow/restart','/flow/submission-status','/flow/cancel'): required_method='POST'
            elif route=='/flow/history' or route.startswith(('/flow/status/','/flow/adopted/','/flow/recovery/')): required_method='GET'
            elif route.startswith('/flow/source/'): required_method='DELETE'
            if required_method is None: self.reply(404,{'error':'not_found'}); return
            if method!=required_method:
                self.send_response(405);self.send_header('Allow',required_method)
                self.send_header('Content-Length','0');self.send_header('Cache-Control','no-store');self.end_headers();return
            owner=f.request_owner
            if route=='/flow/cancel':
                d=self.body(('axiom_id','expected_job_id'))
                self.reply(200,f.cancel_deleted(**d));return
            if route=='/flow/submission-status':
                d=self.body(('request_id',))
                self.reply(200,f.resolve_submission(owner,d['request_id']));return
            if route=='/flow/restart':
                d=self.body(('axiom_id','expected_job_id','request_id','acknowledge_possible_duplicate_charge'))
                self.reply(200,f.restart(**d));return
            if route=='/flow/recover':
                d=self.body(('axiom_id','expected_job_id'));f._identity(d['axiom_id'],d['expected_job_id'])
                with f.atomic():
                    state=f._recovery(d['axiom_id'])
                    if state['job_id']!=d['expected_job_id']:raise Conflict('generation_changed')
                    if not state['can_resume']:raise Conflict('generation_outcome_unknown' if state['state']=='uncertain' else 'source_unavailable')
                    row=f._axiom(d['axiom_id'])
                    job=f.db.execute('SELECT * FROM model_jobs WHERE owner=? AND request_id=?',(owner,state['job_id'])).fetchone()
                if state['state']!='completed':
                    if job and job['output_id']:output_id=job['output_id']
                    else:
                        provider=self.server.provider
                        _,out=f.generate(state['job_id'],[row['source_id']],lambda ctx:provider.generate(ctx,row['ordinal']),provider_version=provider.version,single_dispatch=True)
                        output_id=out['source_id']
                    f.complete(d['axiom_id'],output_id)
                self.deliver_reading(f,d['axiom_id'],state['job_id']);return
            if route in ('/flow/append','/flow/next'):
                d=self.body(('cluster_id','axiom_id','source_id'),('transition_id','expected_araf_version') if route=='/flow/next' else ())
                f._identity(d['cluster_id'],d['axiom_id'],d['source_id'])
                if ('transition_id' in d)!=('expected_araf_version' in d): raise ValueError('transition_pair')
                n=f.start(**d) if route=='/flow/next' else f.append(d['cluster_id'],d['axiom_id'],d['source_id'])
                if route=='/flow/append': self.reply(200,{'axiom_id':d['axiom_id'],'ordinal':n});return
                with f.atomic():
                    if f._current_job(d['axiom_id'])!='flow/next/'+d['axiom_id']:raise Conflict('recovery_required')
                provider=self.server.provider
                _,out=f.generate('flow/next/'+d['axiom_id'],[d['source_id']],
                                 lambda ctx:provider.generate(ctx,n),provider_version=provider.version,
                                 single_dispatch=not provider.demo)
                f.complete(d['axiom_id'],out['source_id'])
                self.deliver_reading(f,d['axiom_id'],'flow/next/'+d['axiom_id']);return
            if route=='/flow/respond':
                d=self.body(('axiom_id','status','expected_version'));f._identity(d['axiom_id'])
                self.reply(200,{'response_version':f.respond(d['axiom_id'],d['status'],d['expected_version'])});return
            if route=='/flow/close':
                d=self.body(('cluster_id','target'));tid=f.close_cluster(d['cluster_id'],d['target'])
                self.reply(200,{'transition_id':tid,'target':d['target'],'status':'araf_open'});return
            if route=='/flow/history':
                # Metadata only, capped at 100; has_more signals truncation. Pagination not yet implemented.
                with f.atomic():
                    rows=f.db.execute('''SELECT id,cluster_id,ordinal,source_id,output_id,completed,response_status,response_version,0 cancelled,rowid history_order FROM flow_axioms WHERE owner=?
                      UNION ALL SELECT id,cluster_id,ordinal,source_id,NULL,0,'unanswered',0,1,history_order FROM flow_cancelled_axioms WHERE owner=?
                      ORDER BY history_order DESC,cancelled LIMIT 101''',(owner,owner)).fetchall()
                    result={'items':[dict(r) for r in rows[:100]],'has_more':len(rows)>100}
                self.reply(200,result);return
            prefix=next(p for p in ('/flow/status/','/flow/adopted/','/flow/source/','/flow/recovery/') if route.startswith(p))
            key=unquote(route[len(prefix):]);f._identity(key)
            if prefix=='/flow/status/': self.reply(200,f.status(key))
            elif prefix=='/flow/recovery/': self.reply(200,f.recovery(key))
            elif prefix=='/flow/adopted/': self.reply(200,{'output_ids':f.adopted(key)})
            else:
                with f.atomic():
                    if f._unit(owner,key)['kind']!='source': raise Conflict('user_source_required')
                self.reply(200,f.delete(owner,key))
        except ProviderError as exc: self.reply(exc.status,{'error':exc.code})
        except Unauthorized: self.reply(401,{'error':'unauthorized'})
        except Conflict as exc: self.reply(409,{'error':exc.code})
        except Unavailable: self.reply(409,{'error':'source_unavailable'})
        except (ValueError,TypeError): self.reply(400,{'error':'invalid_request'})
        except sqlite3.Error: self.reply(503,{'error':'transaction_failed'})
        except OSError: self.close_connection=True
        finally:
            if f is not None:f.close()

    def deliver_reading(self,f,axiom_id,job_id):
        self.server.before_flow_delivery()
        def build():
            owner=f.request_owner;row=f._axiom(axiom_id)
            if f._current_job(axiom_id)!=job_id:raise Conflict('generation_changed')
            job=f.db.execute('SELECT * FROM model_jobs WHERE owner=? AND request_id=?',(owner,job_id)).fetchone()
            if not job or job['output_id']!=row['output_id']:raise Conflict('reading_job_mismatch')
            return {'axiom_id':row['id'],'ordinal':row['ordinal'],'output_id':row['output_id'],
                    'text':f._read(owner,row['output_id']),'reason':f._permissions(owner,row['output_id'])['reason'],
                    'response_status':row['response_status'],'response_version':row['response_version'],
                    'is_node':row['ordinal'] in NODES,'demo':job['provider_version']=='stage2-demo-v1','provider':job['provider_version']}
        deliver(self,f,build)

class ReadingServer(DeliveryServer):
    def __init__(self,path,provider=None):
        self.provider=provider if provider is not None else DemoProvider()
        super().__init__(path)
        self.RequestHandlerClass=ReadingHandler
    def before_flow_delivery(self): pass
