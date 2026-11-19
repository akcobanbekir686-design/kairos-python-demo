"""Trusted, experimental 3+1+1 coordinator. No public UI or live model."""
import json
import uuid
from calculation import Record, select, symbol_ids
from similarity_store import SimilarityStore
from core import Conflict, Unavailable, pack

METHOD='kairos-memory-draft-0.5'

class MemoryStore(SimilarityStore):
    def __init__(self,path,token):
        super().__init__(path,token)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS narrative_times(
          owner TEXT NOT NULL, source_id TEXT NOT NULL, unit_id TEXT NOT NULL,
          PRIMARY KEY(owner,source_id));
        CREATE TABLE IF NOT EXISTS memory_requests(
          owner TEXT NOT NULL, request_id TEXT NOT NULL, source_id TEXT NOT NULL,
          a_result_id TEXT NOT NULL, result_id TEXT NOT NULL,
          PRIMARY KEY(owner,request_id));
        ''')

    def register_event_time(self,source_id,occurred_at_us):
        """Trusted explicit UTC event time, not insertion time. No migration inference."""
        self._identity(source_id)
        if type(occurred_at_us) is not int or occurred_at_us<1:raise ValueError('event_time_required')
        body=pack({'occurred_at_us':occurred_at_us,'method':'explicit-event-time-v1'})
        with self.atomic():
            row,_=self._input(source_id)
            old=self.db.execute('SELECT unit_id FROM narrative_times WHERE owner=? AND source_id=?',
                                (self.request_owner,source_id)).fetchone()
            if old:
                if self._read(self.request_owner,old['unit_id'],True)!=body:raise Conflict('event_time_conflict')
                return old['unit_id']
            uid='event-time:'+uuid.uuid4().hex
            self._add(self.request_owner,uid,body,'narrative_time',[row['metadata_id']])
            self.db.execute('INSERT INTO narrative_times VALUES(?,?,?)',(self.request_owner,source_id,uid))
            return uid

    def select_memory(self,request_id,source_id,a_result_id):
        self._identity(request_id,source_id,a_result_id)
        with self.atomic():
            owner=self.request_owner
            old=self.db.execute('SELECT * FROM memory_requests WHERE owner=? AND request_id=?',
                                (owner,request_id)).fetchone()
            if old:
                if (old['source_id'],old['a_result_id'])!=(source_id,a_result_id):raise Conflict('request_payload_conflict')
                self._read(owner,old['result_id'],True)
                return old['result_id']
            binding=self.db.execute('SELECT source_id FROM similarity_requests WHERE owner=? AND result_id=?',
                                   (owner,a_result_id)).fetchone()
            if binding is None or binding['source_id']!=source_id:raise Conflict('a_binding_mismatch')
            a=json.loads(self._read(owner,a_result_id,True))
            if not a['selection_ready']:raise Conflict('a_not_ready')
            current,data=self._input(source_id)
            if current['state']!='open' or data['origin']!='user_narrative' or data['entertainment']:
                raise Conflict('current_narrative_required')
            if data['symbols'] is None:raise Conflict('current_symbols_missing')
            symbols=symbol_ids(data['symbols']);records={};times={};overlap={}
            parents=[a_result_id,current['metadata_id']]
            rows=self.db.execute('''SELECT n.source_id FROM narrative_inputs n
              JOIN narrative_sessions s ON s.owner=n.owner AND s.id=n.session_id
              WHERE n.owner=? AND n.session_id!=? AND s.state='completed' ORDER BY n.source_id''',
              (owner,current['session_id'])).fetchall()
            for item in rows:
                try:r,d=self._input(item['source_id'])
                except Unavailable:continue
                if d['origin']!='user_narrative' or d['entertainment']:continue
                sid=r['source_id'];parents.append(r['metadata_id'])
                if d['symbols'] is None or d['dictionary_version']!=data['dictionary_version']:
                    raise Conflict('history_symbols_incomplete')
                t=self.db.execute('SELECT unit_id FROM narrative_times WHERE owner=? AND source_id=?',
                                  (owner,sid)).fetchone()
                if t is None:raise Conflict('history_time_missing')
                times[sid]=json.loads(self._read(owner,t['unit_id'],True))['occurred_at_us']
                parents.append(t['unit_id'])
                records[sid]=Record(sid,owner,r['session_id'],tuple(d['symbols']),event_group=d['event_group'])
                overlap[sid]=len(symbols & symbol_ids(d['symbols']))
            if set(records)!=(set(a['dependency_source_ids'])-{source_id}):
                raise Conflict('a_snapshot_history_changed')
            # Experimental, explicit ordering; no undocumented timestamp fallback.
            apool=[records[r['source_id']] for r in a['pool']]
            sp=[records[sid] for sid in sorted(records,key=lambda s:(-overlap[s],-times[s],s)) if overlap[sid]>0]
            rp=[records[sid] for sid in sorted(records,key=lambda s:(-times[s],s))]
            result=select({'A':apool,'symbol':sp,'recent':rp},owner=owner,current_session=current['session_id'])
            result.update(coordinator_method=METHOD,current_source_id=source_id,a_result_id=a_result_id,
                          dictionary_version=data['dictionary_version'],
                          ordering={'symbol':'distinct_overlap_desc,event_time_desc,source_id_asc',
                                    'recent':'event_time_desc,source_id_asc'},
                          candidates=[dict(source_id=sid,symbol_overlap=overlap[sid],occurred_at_us=times[sid]) for sid in sorted(records)])
            body=pack(result)
            if len(body.encode())>262144:raise ValueError('selection_size')
            uid='memory:'+uuid.uuid4().hex
            self._add(owner,uid,body,'memory_selection',sorted(set(parents)))
            self.db.execute('INSERT INTO memory_requests VALUES(?,?,?,?,?)',(owner,request_id,source_id,a_result_id,uid))
            return uid

    def generate_with_memory(self,axiom_id,request_id,selection_id,provider,*,provider_version):
        """Local provider gets typed current/history/selection, not an unordered blob.
        All bodies are reread by generate; final output inherits every dependency.
        """
        self._identity(axiom_id,request_id,selection_id,provider_version)
        with self.atomic():
            axiom=self._axiom(axiom_id)
            binding=self.db.execute('SELECT source_id FROM memory_requests WHERE owner=? AND result_id=?',
                                   (self.request_owner,selection_id)).fetchone()
            if binding is None or binding['source_id']!=axiom['source_id']:raise Conflict('memory_binding_mismatch')
            selection=json.loads(self._read(self.request_owner,selection_id,True))
            source_id=axiom['source_id']
            selected_ids=[r['source_id'] for r in selection['selected']]
        def typed(context):
            by_id={c.source_id:c.text for c in context}
            trace=json.loads(by_id[selection_id])
            return provider({'current':{'source_id':source_id,'text':by_id[source_id]},
                             'history':[dict(item,text=by_id[item['source_id']]) for item in trace['selected']],
                             'selection':trace,'kind':'memory-context-v1'})
        replay,out=self.generate(request_id,[source_id,selection_id]+selected_ids,typed,
                                 provider_version=provider_version+'|'+METHOD,single_dispatch=True)
        self.complete(axiom_id,out['source_id'])
        return replay,out
