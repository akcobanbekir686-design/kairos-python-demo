"""Persistent experimental A snapshots over the existing deletion graph.
Trusted server API only. No embedding model, HTTP endpoint or provider change.
"""
import json
import uuid
from dataclasses import asdict
from narrative_store import NarrativeStore
from history_similarity import Embedding, rank_history, unit
from calculation import Record
from core import Conflict, Unavailable, pack

class SimilarityStore(NarrativeStore):
    def __init__(self, path, token):
        super().__init__(path, token)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS narrative_vectors(
          owner TEXT NOT NULL, source_id TEXT NOT NULL, model TEXT NOT NULL,
          preprocessing TEXT NOT NULL, unit_id TEXT NOT NULL,
          PRIMARY KEY(owner,source_id,model,preprocessing));
        CREATE TABLE IF NOT EXISTS similarity_requests(
          owner TEXT NOT NULL, request_id TEXT NOT NULL, source_id TEXT NOT NULL,
          model TEXT NOT NULL, preprocessing TEXT NOT NULL, result_id TEXT NOT NULL,
          PRIMARY KEY(owner,request_id));
        ''')

    def register_vector(self, source_id, vector, *, model_revision, preprocessing_revision):
        self._identity(source_id,model_revision,preprocessing_revision)
        vector=tuple(vector) if isinstance(vector,(tuple,list)) else vector
        _,reason=unit(vector)
        if reason:raise ValueError(reason)
        body=pack(dict(vector=vector,model_revision=model_revision,
                       preprocessing_revision=preprocessing_revision))
        if len(body.encode())>65536:raise ValueError('vector_size')
        with self.atomic():
            row,data=self._input(source_id)
            if data['origin']!='user_narrative' or data['entertainment']:
                raise Conflict('narrative_vector_required')
            args=(self.request_owner,source_id,model_revision,preprocessing_revision)
            old=self.db.execute('''SELECT unit_id FROM narrative_vectors
                WHERE owner=? AND source_id=? AND model=? AND preprocessing=?''',args).fetchone()
            if old:
                if self._read(self.request_owner,old['unit_id'],True)!=body:
                    raise Conflict('vector_identity_conflict')
                return old['unit_id']
            uid='vector:'+uuid.uuid4().hex
            self._add(self.request_owner,uid,body,'narrative_vector',[row['metadata_id']])
            self.db.execute('INSERT INTO narrative_vectors VALUES(?,?,?,?,?)',args+(uid,))
            return uid

    def _embedding(self, row, data, model, preprocessing, parents):
        parents.append(row['metadata_id'])
        v=self.db.execute('''SELECT unit_id FROM narrative_vectors
            WHERE owner=? AND source_id=? AND model=? AND preprocessing=?''',
            (self.request_owner,row['source_id'],model,preprocessing)).fetchone()
        vector=None
        if v:
            # Existing but unavailable vectors block this snapshot; never become zero.
            vector=tuple(json.loads(self._read(self.request_owner,v['unit_id'],True))['vector'])
            parents.append(v['unit_id'])
        return Embedding(Record(row['source_id'],self.request_owner,row['session_id'],
            tuple(data['symbols'] or ()),origin=data['origin'],session_state=row['state'],
            entertainment=data['entertainment'],event_group=data['event_group']),
            vector,model,preprocessing)

    def calculate_a(self, request_id, source_id, *, model_revision, preprocessing_revision):
        """Immutable as-of ranking; result ID only, delivered via existing /content.
        Same request cannot change source/revisions. Missing vectors remain explicit.
        A-only pool: no fabricated symbol or recent-channel ordering.
        """
        self._identity(request_id,source_id,model_revision,preprocessing_revision)
        with self.atomic():
            owner=self.request_owner
            old=self.db.execute('SELECT * FROM similarity_requests WHERE owner=? AND request_id=?',
                                (owner,request_id)).fetchone()
            if old:
                if (old['source_id'],old['model'],old['preprocessing'])!=(source_id,model_revision,preprocessing_revision):
                    raise Conflict('request_payload_conflict')
                self._read(owner,old['result_id'],True)
                return old['result_id']
            row,data=self._input(source_id)
            parents=[]
            current=self._embedding(row,data,model_revision,preprocessing_revision,parents)
            history=[]
            rows=self.db.execute('''SELECT n.source_id FROM narrative_inputs n
                JOIN narrative_sessions s ON s.owner=n.owner AND s.id=n.session_id
                WHERE n.owner=? AND n.session_id!=? AND s.state='completed'
                ORDER BY n.source_id''',(owner,row['session_id'])).fetchall()
            for item in rows:
                try:past,annotation=self._input(item['source_id'])
                except Unavailable:continue
                if annotation['origin']!='user_narrative' or annotation['entertainment']:continue
                history.append(self._embedding(past,annotation,model_revision,preprocessing_revision,parents))
            result=rank_history(current,history,owner=owner,current_session=row['session_id'])
            result['pool']=[asdict(r) for r in result['pool']]
            result['snapshot_scope']='eligible_completed_sessions_at_request'
            result['selection_ready']=result['state'] in ('ranked','no_history')
            body=pack(result)
            if len(body.encode())>262144:raise ValueError('similarity_result_size')
            uid='similarity:'+uuid.uuid4().hex
            self._add(owner,uid,body,'similarity_output',sorted(set(parents)))
            self.db.execute('INSERT INTO similarity_requests VALUES(?,?,?,?,?,?)',
                (owner,request_id,source_id,model_revision,preprocessing_revision,uid))
            return uid
