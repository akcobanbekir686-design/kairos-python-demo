"""Unified T + memory snapshot contract. Trusted local coordinator, no live model."""
import json
import uuid
from memory_store import MemoryStore
from core import Conflict,pack

METHOD='kairos-response-0.6'
class ResponseStore(MemoryStore):
    def __init__(self,path,token):
        super().__init__(path,token)
        self.db.execute('''CREATE TABLE IF NOT EXISTS response_requests(
          owner TEXT NOT NULL, request_id TEXT NOT NULL, source_id TEXT NOT NULL,
          t_id TEXT NOT NULL, memory_id TEXT NOT NULL, result_id TEXT NOT NULL,
          PRIMARY KEY(owner,request_id))''')
    def _metadata_ancestors(self,uid):
        return {i for i in self._ancestors(self.request_owner,uid)
                if self._unit(self.request_owner,i)['kind']=='narrative_metadata'}
    def prepare_response(self,request_id,source_id,t_id,memory_id):
        self._identity(request_id,source_id,t_id,memory_id)
        with self.atomic():
            owner=self.request_owner
            old=self.db.execute('SELECT * FROM response_requests WHERE owner=? AND request_id=?',
                                (owner,request_id)).fetchone()
            if old:
                if (old['source_id'],old['t_id'],old['memory_id'])!=(source_id,t_id,memory_id):
                    raise Conflict('request_payload_conflict')
                self._read(owner,old['result_id'],True);return old['result_id']
            for table,uid in (('calculation_requests',t_id),('memory_requests',memory_id)):
                row=self.db.execute('SELECT source_id FROM '+table+' WHERE owner=? AND result_id=?',(owner,uid)).fetchone()
                if row is None or row['source_id']!=source_id:raise Conflict('response_binding_mismatch')
            t=json.loads(self._read(owner,t_id,True));m=json.loads(self._read(owner,memory_id,True))
            current,data=self._input(source_id)
            if current['state']!='open':raise Conflict('current_session_not_open')
            if t['state'] not in ('calculated','empty'):raise Conflict('t_not_ready')
            if t['dictionary_version']!=m['dictionary_version']:raise Conflict('dictionary_mismatch')
            if self._metadata_ancestors(t_id)!=self._metadata_ancestors(memory_id):
                raise Conflict('snapshot_history_mismatch')
            body=pack(dict(method=METHOD,current_source_id=source_id,t_result_id=t_id,memory_result_id=memory_id,
                calculations={'T':t},memory=m,
                console=[{'stage':'T','status':'completed','state':t['state']},
                         {'stage':'memory','status':'completed','selected_count':len(m['selected'])},
                         {'stage':'snapshot','status':'completed','same_history':True}],
                snapshot_contract='same_immutable_narrative_metadata_set'))
            if len(body.encode())>524288:raise ValueError('response_size')
            uid='response:'+uuid.uuid4().hex
            self._add(owner,uid,body,'response_snapshot',[t_id,memory_id])
            self.db.execute('INSERT INTO response_requests VALUES(?,?,?,?,?,?)',(owner,request_id,source_id,t_id,memory_id,uid))
            return uid
    def generate_response(self,axiom_id,request_id,snapshot_id,provider,*,provider_version):
        self._identity(axiom_id,request_id,snapshot_id,provider_version)
        with self.atomic():
            ax=self._axiom(axiom_id)
            binding=self.db.execute('SELECT source_id FROM response_requests WHERE owner=? AND result_id=?',
                                   (self.request_owner,snapshot_id)).fetchone()
            if binding is None or binding['source_id']!=ax['source_id']:raise Conflict('response_binding_mismatch')
            bundle=json.loads(self._read(self.request_owner,snapshot_id,True));source_id=ax['source_id']
            selected=[r['source_id'] for r in bundle['memory']['selected']]
        def typed(context):
            bodies={r.source_id:r.text for r in context};b=json.loads(bodies[snapshot_id])
            return provider(dict(kind='kairos-response-context-v1',current={'source_id':source_id,'text':bodies[source_id]},
                history=[dict(r,text=bodies[r['source_id']]) for r in b['memory']['selected']],
                calculations=b['calculations'],selection=b['memory'],method=b['method']))
        replay,out=self.generate(request_id,[source_id,snapshot_id]+selected,typed,
                                 provider_version=provider_version+'|'+METHOD,single_dispatch=True)
        self.complete(axiom_id,out['source_id']);return replay,out
