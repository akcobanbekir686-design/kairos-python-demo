"""Explicit recovery. A new authorized attempt is not a retry of the uncertain job."""
import uuid
from core import Conflict, Unavailable
from reading_flow import ReadingFlow

class RecoveryFlow(ReadingFlow):
    def __init__(self,path,token):
        super().__init__(path,token)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS flow_generation_heads(
          owner TEXT, axiom_id TEXT, job_id TEXT, PRIMARY KEY(owner,axiom_id));
        CREATE TABLE IF NOT EXISTS flow_restart_requests(
          owner TEXT, request_id TEXT, axiom_id TEXT, previous_job TEXT, new_job TEXT,
          acknowledged INTEGER NOT NULL, PRIMARY KEY(owner,request_id));
        ''')
    def _current_job(self,axiom_id):
        row=self.db.execute('SELECT job_id FROM flow_generation_heads WHERE owner=? AND axiom_id=?',(self.request_owner,axiom_id)).fetchone()
        return row[0] if row else 'flow/next/'+axiom_id
    def _recovery(self,axiom_id):
        if self.db.execute('SELECT 1 FROM flow_cancelled_axioms WHERE owner=? AND id=?',(self.request_owner,axiom_id)).fetchone():
            return {'axiom_id':axiom_id,'job_id':self._current_job(axiom_id),'state':'cancelled','can_resume':False,'can_start_new':False,'can_cancel':False}
        row=self._axiom(axiom_id);job_id=self._current_job(axiom_id)
        job=self.db.execute('SELECT * FROM model_jobs WHERE owner=? AND request_id=?',(self.request_owner,job_id)).fetchone()
        claim=self.db.execute('SELECT state FROM model_attempts WHERE owner=? AND request_id=?',(self.request_owner,job_id)).fetchone()
        try:
            if row['completed']:self._read(self.request_owner,row['output_id'])
            else:self._source(row['source_id'])
        except Unavailable:state='source_unavailable'
        else:
            if row['completed']:state='completed'
            elif job and job['output_id']:state='result_ready'
            elif job is None:state='ready'
            elif claim and claim['state']=='retryable':state='retryable'
            else:state='uncertain'  # dispatched may still be running; never infer remote termination.
        return {'axiom_id':axiom_id,'job_id':job_id,'state':state,
                'can_resume':state in ('ready','retryable','result_ready','completed'),
                'can_start_new':state=='uncertain',
                'can_cancel':not row['completed'] and self._permissions(self.request_owner,row['source_id'])['reason']=='deleted'}

    def cancel_deleted(self,axiom_id,expected_job_id):
        self._identity(axiom_id,expected_job_id)
        owner=self.request_owner
        with self.atomic():
            if self._current_job(axiom_id)!=expected_job_id:raise Conflict('generation_changed')
            if self.db.execute('SELECT 1 FROM flow_cancelled_axioms WHERE owner=? AND id=?',(owner,axiom_id)).fetchone():
                return {'axiom_id':axiom_id,'status':'cancelled'}
            r=self._axiom(axiom_id)
            if r['completed'] or self._permissions(owner,r['source_id'])['reason']!='deleted':
                raise Conflict('cancellation_not_allowed')
            last=self.db.execute('SELECT id FROM flow_axioms WHERE owner=? AND cluster_id=? ORDER BY ordinal DESC LIMIT 1',(owner,r['cluster_id'])).fetchone()
            if last['id']!=axiom_id:raise Conflict('not_latest_axiom')
            self.db.execute("INSERT INTO model_attempts VALUES(?,?,'superseded',0) ON CONFLICT(owner,request_id) DO UPDATE SET state='superseded'",(owner,expected_job_id))
            self.db.execute('''INSERT INTO flow_cancelled_axioms SELECT owner,id,cluster_id,ordinal,source_id,rowid
              FROM flow_axioms WHERE owner=? AND id=?''',(owner,axiom_id))
            self.db.execute('DELETE FROM flow_axioms WHERE owner=? AND id=?',(owner,axiom_id))
            return {'axiom_id':axiom_id,'status':'cancelled'}
    def recovery(self,axiom_id):
        self._identity(axiom_id)
        with self.atomic():return self._recovery(axiom_id)
    def restart(self,axiom_id,expected_job_id,request_id,acknowledge_possible_duplicate_charge):
        self._identity(axiom_id,expected_job_id,request_id)
        if acknowledge_possible_duplicate_charge is not True:raise ValueError('explicit_acknowledgement_required')
        owner=self.request_owner
        with self.atomic():
            row=self._axiom(axiom_id);self._source(row['source_id'])
            old=self.db.execute('SELECT * FROM flow_restart_requests WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if old:
                if (old['axiom_id'],old['previous_job'])!=(axiom_id,expected_job_id):raise Conflict('request_payload_conflict')
                return {'axiom_id':axiom_id,'job_id':old['new_job'],'status':'new_attempt_authorized'}
            if self._current_job(axiom_id)!=expected_job_id:raise Conflict('generation_changed')
            state=self._recovery(axiom_id)
            if not state['can_start_new']:raise Conflict('new_attempt_not_allowed')
            # Global ModelStore fence also blocks callbacks that started before this transaction.
            self.db.execute("INSERT INTO model_attempts VALUES(?,?,'superseded',0) ON CONFLICT(owner,request_id) DO UPDATE SET state='superseded'",(owner,expected_job_id))
            job_id='flow/restart/'+uuid.uuid4().hex
            self.db.execute('INSERT INTO flow_generation_heads VALUES(?,?,?) ON CONFLICT(owner,axiom_id) DO UPDATE SET job_id=excluded.job_id',(owner,axiom_id,job_id))
            self.db.execute('INSERT INTO flow_restart_requests VALUES(?,?,?,?,?,1)',(owner,request_id,axiom_id,expected_job_id,job_id))
            return {'axiom_id':axiom_id,'job_id':job_id,'status':'new_attempt_authorized'}
