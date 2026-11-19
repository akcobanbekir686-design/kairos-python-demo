"""Trusted calculation coordinator over the v0.3.9 SQLite/provenance core.
No public endpoints, automatic extraction, model choice or session auto-completion.
All personal symbol data and result bodies live in deletable dependency units.
"""
import json
import uuid
from calculation import Record, repetition, symbol_ids, VERSION
from core import Conflict, Unavailable, pack
from reading_flow import ReadingFlow

INPUT_METHOD = 'narrative-input-v1'

class NarrativeStore(ReadingFlow):
    def __init__(self, path, token):
        super().__init__(path, token)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS narrative_sessions(
          owner TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL,
          PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS narrative_inputs(
          owner TEXT NOT NULL, source_id TEXT NOT NULL, session_id TEXT NOT NULL,
          metadata_id TEXT NOT NULL, PRIMARY KEY(owner,source_id));
        CREATE TABLE IF NOT EXISTS calculation_requests(
          owner TEXT NOT NULL, request_id TEXT NOT NULL, source_id TEXT NOT NULL,
          result_id TEXT NOT NULL, PRIMARY KEY(owner,request_id));
        ''')

    def open_narrative_session(self, session_id):
        self._identity(session_id)
        with self.atomic():
            row=self.db.execute('SELECT state FROM narrative_sessions WHERE owner=? AND id=?',
                                (self.request_owner,session_id)).fetchone()
            if row:
                if row['state']!='open':raise Conflict('narrative_session_closed')
                return
            self.db.execute('INSERT INTO narrative_sessions VALUES(?,?,?)',
                            (self.request_owner,session_id,'open'))

    def close_narrative_session(self, session_id, state):
        """Trusted explicit lifecycle. Does not infer delivered/accepted from completed.
        completed/incomplete are terminal in this initial adapter.
        """
        self._identity(session_id)
        if state not in ('completed','incomplete'):raise ValueError('session_state')
        with self.atomic():
            row=self._narrative_session(session_id)
            if row['state']==state:return
            if row['state']!='open':raise Conflict('narrative_session_closed')
            if state=='completed' and not self.db.execute(
                'SELECT 1 FROM narrative_inputs WHERE owner=? AND session_id=?',
                (self.request_owner,session_id)).fetchone():raise Conflict('empty_session')
            self.db.execute('UPDATE narrative_sessions SET state=? WHERE owner=? AND id=?',
                            (state,self.request_owner,session_id))

    def _narrative_session(self, session_id):
        row=self.db.execute('SELECT * FROM narrative_sessions WHERE owner=? AND id=?',
                            (self.request_owner,session_id)).fetchone()
        if row is None:raise Unavailable('narrative_session_unavailable')
        return row

    def _user_source(self, source_id):
        self._source(source_id)
        # Raw Core.source fixtures are not proof of a user submission.
        if not self.db.execute('SELECT 1 FROM origin_requests WHERE owner=? AND unit_id=?',
                               (self.request_owner,source_id)).fetchone():
            raise Conflict('user_submission_required')
        if self.db.execute('SELECT 1 FROM edges WHERE owner=? AND child=?',
                           (self.request_owner,source_id)).fetchone():
            raise Conflict('independent_narrative_required')

    def register_narrative(self, source_id, session_id, symbols, *, dictionary_version,
                           origin='user_narrative', entertainment=False, event_group=None):
        """Trusted annotation, immutable. Symbols None means extractor unavailable.
        IDs must already belong to the named canonical dictionary; no extraction here.
        Replay checks live permissions and cannot resurrect deleted metadata.
        """
        self._identity(source_id,session_id,dictionary_version)
        if origin not in ('user_narrative','tarot_draw'):raise ValueError('origin')
        if type(entertainment) is not bool:raise ValueError('entertainment')
        if event_group is not None:self._identity(event_group)
        normalized=None if symbols is None else sorted(symbol_ids(symbols))
        payload=pack(dict(method=INPUT_METHOD,symbols=normalized,dictionary_version=dictionary_version,
                          origin=origin,entertainment=entertainment,event_group=event_group))
        if len(payload.encode())>32768:raise ValueError('annotation_size')
        with self.atomic():
            self._user_source(source_id)
            session=self._narrative_session(session_id)
            old=self.db.execute('SELECT * FROM narrative_inputs WHERE owner=? AND source_id=?',
                                (self.request_owner,source_id)).fetchone()
            if old:
                existing=self._read(self.request_owner,old['metadata_id'],True)
                if old['session_id']!=session_id or existing!=payload:raise Conflict('narrative_identity_conflict')
                return old['metadata_id']
            if session['state']!='open':raise Conflict('narrative_session_closed')
            uid='narrative:'+uuid.uuid4().hex
            self._add(self.request_owner,uid,payload,'narrative_metadata',[source_id])
            self.db.execute('INSERT INTO narrative_inputs VALUES(?,?,?,?)',
                            (self.request_owner,source_id,session_id,uid))
            return uid

    def _input(self, source_id):
        row=self.db.execute('''SELECT n.*,s.state FROM narrative_inputs n
          JOIN narrative_sessions s ON s.owner=n.owner AND s.id=n.session_id
          WHERE n.owner=? AND n.source_id=?''',(self.request_owner,source_id)).fetchone()
        if row is None:raise Unavailable('narrative_input_unavailable')
        self._user_source(source_id)
        data=json.loads(self._read(self.request_owner,row['metadata_id'],True))
        return row,data

    def calculate_t(self, request_id, source_id):
        """Persist an immutable as-of snapshot. Return only an ID for /content delivery.
        All usable eligible examined inputs, including nonmatches, are dependencies.
        New request IDs make new snapshots; a successful retry reuses its old snapshot.
        """
        self._identity(request_id,source_id)
        with self.atomic():
            owner=self.request_owner
            old=self.db.execute('SELECT * FROM calculation_requests WHERE owner=? AND request_id=?',
                                (owner,request_id)).fetchone()
            if old:
                if old['source_id']!=source_id:raise Conflict('request_payload_conflict')
                self._read(owner,old['result_id'],True)
                return old['result_id']
            current, data=self._input(source_id)
            if current['state']!='open':raise Conflict('current_session_not_open')
            if data['origin']!='user_narrative' or data['entertainment']:
                raise Conflict('narrative_calculation_required')
            parents=[current['metadata_id']]; records=[]; omitted=0; mismatched=0
            rows=self.db.execute('''SELECT n.source_id FROM narrative_inputs n
              JOIN narrative_sessions s ON s.owner=n.owner AND s.id=n.session_id
              WHERE n.owner=? AND n.session_id!=? AND s.state='completed'
              ORDER BY n.source_id''',(owner,current['session_id'])).fetchall()
            for item in rows:
                try:r,d=self._input(item['source_id'])
                except Unavailable:continue
                if d['origin']!='user_narrative' or d['entertainment']:continue
                # Missing extraction / incompatible dictionaries are uncertainty, not zero.
                parents.append(r['metadata_id'])
                if d['symbols'] is None:omitted+=1;continue
                if d['dictionary_version']!=data['dictionary_version']:mismatched+=1;continue
                records.append(Record(r['source_id'],owner,r['session_id'],tuple(d['symbols']),
                                      event_group=d['event_group']))
            result=repetition(data['symbols'],records,owner=owner,current_session=current['session_id'])
            value=result.pop('value')
            result['ratio']=None if value is None else {'numerator':value.numerator,'denominator':value.denominator}
            result['dictionary_version']=data['dictionary_version']
            result['history_coverage']={'compatible_records':len(records),
                                       'unavailable_symbols':omitted,'incompatible_dictionary':mismatched}
            if result['state']=='calculated' and (omitted or mismatched):
                # Do not publish a seemingly definitive T from partial historical inputs.
                result['state']='history_incomplete';result['ratio']=None
                result['numerator']=None;result['denominator']=len(symbol_ids(data['symbols']))
                result.pop('evidence',None);result['source_ids']=[]
            result['snapshot_scope']='eligible_completed_sessions_at_request'
            body=pack(result)
            if len(body.encode())>65536:raise ValueError('calculation_result_size')
            uid='calculation:'+uuid.uuid4().hex
            self._add(owner,uid,body,'calculation_output',sorted(set(parents)))
            self.db.execute('INSERT INTO calculation_requests VALUES(?,?,?,?)',(owner,request_id,source_id,uid))
            return uid

    def generate_with_t(self, axiom_id, request_id, result_id, provider, *, provider_version):
        """Trusted integration path; provider sees current narrative and typed T JSON.
        Not the complete memory selector. Does not modify default HTTP/demo behavior.
        All contexts remain dependencies through ModelStore.generate and result edges.
        """
        self._identity(axiom_id,request_id,result_id,provider_version)
        with self.atomic():
            row=self._axiom(axiom_id)
            binding=self.db.execute('SELECT source_id FROM calculation_requests WHERE owner=? AND result_id=?',
                                    (self.request_owner,result_id)).fetchone()
            if binding is None or binding['source_id']!=row['source_id']:raise Conflict('calculation_binding_mismatch')
            self._read(self.request_owner,result_id,True)
            source_id=row['source_id']
        replay,out=self.generate(request_id,[source_id,result_id],provider,
                                 provider_version=provider_version+'|'+VERSION,single_dispatch=True)
        self.complete(axiom_id,out['source_id'])
        return replay,out
