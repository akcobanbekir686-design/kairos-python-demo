"""Local persistent reading coordinator, no UI/live provider.
Completion is a trusted server event, never a client approval assertion.
"""
import uuid
from core import Conflict, Unavailable, pack
from model_flow import ModelStore

NODES=(1,2,3,5,8,13,21)

class ReadingFlow(ModelStore):
    def __init__(self,path,token):
        super().__init__(path,token)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS flow_clusters(
          owner TEXT, id TEXT, closed INTEGER NOT NULL DEFAULT 0,
          transition_id TEXT, target TEXT, PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS flow_axioms(
          owner TEXT, id TEXT, cluster_id TEXT, ordinal INTEGER,
          source_id TEXT, output_id TEXT, completed INTEGER NOT NULL DEFAULT 0,
          response_status TEXT NOT NULL DEFAULT 'unanswered', response_version INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(owner,id), UNIQUE(owner,cluster_id,ordinal), UNIQUE(owner,source_id));
        CREATE TABLE IF NOT EXISTS flow_cancelled_axioms(
          owner TEXT, id TEXT, cluster_id TEXT, ordinal INTEGER, source_id TEXT,
          history_order INTEGER, PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS flow_seed_links(
          owner TEXT, axiom_id TEXT, transition_id TEXT, version INTEGER, trace TEXT,
          PRIMARY KEY(owner,axiom_id));
        ''')

    def _identity(self,*values):
        if any(not isinstance(v,str) or not v.strip() for v in values): raise ValueError('identity')

    def _axiom(self,aid):
        row=self.db.execute('SELECT * FROM flow_axioms WHERE owner=? AND id=?',(self.request_owner,aid)).fetchone()
        if row is None: raise Unavailable('axiom_unavailable')
        return row

    def _source(self,uid):
        self._read(self.request_owner,uid,True)
        if self._unit(self.request_owner,uid)['kind']!='source': raise Conflict('user_input_required')

    def append(self,cluster_id,axiom_id,source_id):
        self._identity(cluster_id,axiom_id,source_id)
        owner=self.request_owner
        with self.atomic():
            return self._append(cluster_id,axiom_id,source_id)

    def _append(self,cluster_id,axiom_id,source_id):
        """Internal: caller must own the write transaction."""
        owner=self.request_owner
        if self.db.execute('SELECT 1 FROM flow_cancelled_axioms WHERE owner=? AND id=?',(owner,axiom_id)).fetchone():
            raise Conflict('axiom_cancelled')
        self._source(source_id)
        old=self.db.execute('SELECT * FROM flow_axioms WHERE owner=? AND id=?',(owner,axiom_id)).fetchone()
        if old:
            if (old['cluster_id'],old['source_id'])!=(cluster_id,source_id): raise Conflict('axiom_identity_conflict')
            return old['ordinal']
        cluster=self.db.execute('SELECT * FROM flow_clusters WHERE owner=? AND id=?',(owner,cluster_id)).fetchone()
        if cluster is None:
            # First cluster only. Subsequent cluster starts use existing Araf/B1 records.
            count=self.db.execute('SELECT count(*) FROM flow_clusters WHERE owner=?',(owner,)).fetchone()[0]
            if count:
                b=self.db.execute('''SELECT b.* FROM beginnings b JOIN transitions t ON t.id=b.tid
                  JOIN flow_clusters f ON f.owner=t.owner AND f.transition_id=t.id
                  WHERE b.owner=? AND t.target=? AND b.axiom=? AND b.trace=? AND f.closed=1''',
                  (owner,cluster_id,axiom_id,source_id)).fetchone()
                if b is None: raise Conflict('beginning_required')
            self.db.execute('INSERT INTO flow_clusters(owner,id) VALUES(?,?)',(owner,cluster_id))
        elif cluster['closed']: raise Conflict('cluster_closed')
        prev=self.db.execute('SELECT * FROM flow_axioms WHERE owner=? AND cluster_id=? ORDER BY ordinal DESC LIMIT 1',
                             (owner,cluster_id)).fetchone()
        if prev and not prev['completed']: raise Conflict('reading_incomplete')
        ordinal=prev['ordinal']+1 if prev else 1
        if ordinal>21: raise Conflict('cluster_full')
        if ordinal==1 and cluster is not None:
            # An empty subsequent cluster may follow cancellation of its first attempt.
            # Preserve the original beginning; bind the new trace separately to its version.
            b=self.db.execute('''SELECT b.* FROM beginnings b JOIN transitions t ON t.id=b.tid
              WHERE b.owner=? AND t.target=?''',(owner,cluster_id)).fetchone()
            if b:
                cancelled=self.db.execute('SELECT 1 FROM flow_cancelled_axioms WHERE owner=? AND id=?',(owner,b['axiom'])).fetchone()
                if not cancelled: raise Conflict('beginning_required')
                self.db.execute('INSERT INTO flow_seed_links VALUES(?,?,?,?,?)',(owner,axiom_id,b['tid'],b['version'],source_id))
        if self.db.execute('SELECT 1 FROM flow_axioms WHERE owner=? AND source_id=?',(owner,source_id)).fetchone():
            raise Conflict('source_already_counted')
        self.db.execute('INSERT INTO flow_axioms(owner,id,cluster_id,ordinal,source_id) VALUES(?,?,?,?,?)',
                        (owner,axiom_id,cluster_id,ordinal,source_id))
        return ordinal

    def start(self,cluster_id,axiom_id,source_id,transition_id=None,expected_araf_version=None):
        self._identity(cluster_id,axiom_id,source_id)
        if (transition_id is None)!=(expected_araf_version is None):raise ValueError('transition_pair')
        if transition_id is not None:
            self._identity(transition_id)
            if type(expected_araf_version) is not int or expected_araf_version<1:raise ValueError('version')
        with self.atomic():
            if transition_id is not None:
                t=self._transition(self.request_owner,transition_id)
                if t['target']!=cluster_id:raise Conflict('transition_target_mismatch')
                if not self.db.execute('SELECT 1 FROM flow_clusters WHERE owner=? AND transition_id=? AND closed=1',(self.request_owner,transition_id)).fetchone():raise Conflict('closed_cluster_required')
                self._begin(self.request_owner,transition_id,axiom_id,source_id,expected_araf_version=expected_araf_version)
            return self._append(cluster_id,axiom_id,source_id)

    def complete(self,axiom_id,output_id):
        """Trusted coordinator attaches a persisted model output; not a public completion flag."""
        with self.atomic():
            row=self._axiom(axiom_id); self._source(row['source_id'])
            self._read(self.request_owner,output_id,True)
            if self._unit(self.request_owner,output_id)['kind']!='model_output': raise Conflict('model_output_required')
            if row['source_id'] not in self._ancestors(self.request_owner,output_id): raise Conflict('reading_source_mismatch')
            if row['completed']:
                if row['output_id']!=output_id: raise Conflict('reading_identity_conflict')
                return
            self.db.execute('UPDATE flow_axioms SET completed=1,output_id=? WHERE owner=? AND id=?',
                            (output_id,self.request_owner,axiom_id))

    def respond(self,axiom_id,status,expected_version):
        # User text corrections intentionally await a separate provenance-aware API.
        if status not in ('accepted','rejected') or type(expected_version) is not int: raise ValueError('response')
        with self.atomic():
            row=self._axiom(axiom_id)
            if not row['completed'] or row['ordinal'] not in NODES: raise Conflict('letter_required')
            self._read(self.request_owner,row['output_id'])
            if row['response_version']!=expected_version:
                if row['response_version']==expected_version+1 and row['response_status']==status: return row['response_version']
                raise Conflict('response_version_conflict')
            self.db.execute('UPDATE flow_axioms SET response_status=?,response_version=response_version+1 WHERE owner=? AND id=?',
                            (status,self.request_owner,axiom_id))
            return expected_version+1

    def status(self,axiom_id):
        with self.atomic():
            cancelled=self.db.execute('SELECT id,ordinal FROM flow_cancelled_axioms WHERE owner=? AND id=?',(self.request_owner,axiom_id)).fetchone()
            if cancelled:return dict(cancelled,completed=0,output_id=None,response_status='unanswered',response_version=0,cancelled=True)
            r=self._axiom(axiom_id)
            return {k:r[k] for k in ('id','ordinal','completed','response_status','response_version','output_id')}

    def adopted(self,cluster_id):
        with self.atomic():
            ids=[]
            for r in self.db.execute("SELECT output_id FROM flow_axioms WHERE owner=? AND cluster_id=? AND response_status='accepted'",(self.request_owner,cluster_id)):
                try: self._read(self.request_owner,r[0])
                except Unavailable: continue
                ids.append(r[0])
            return ids

    def narrative_candidates(self,current_cluster):
        """Eligibility only, not A/T scoring; conservatively excludes entire current cluster."""
        with self.atomic():
            ids=[]
            for r in self.db.execute('SELECT source_id FROM flow_axioms WHERE owner=? AND cluster_id!=? AND completed=1 ORDER BY rowid',
                                     (self.request_owner,current_cluster)):
                try: self._source(r[0])
                except Unavailable: continue
                ids.append(r[0])
            return ids

    def close_cluster(self,cluster_id,target):
        self._identity(cluster_id,target)
        if cluster_id==target: raise ValueError('same_cluster')
        owner=self.request_owner
        with self.atomic():
            cluster=self.db.execute('SELECT * FROM flow_clusters WHERE owner=? AND id=?',(owner,cluster_id)).fetchone()
            if cluster is None: raise Unavailable('cluster_unavailable')
            if cluster['closed']:
                if cluster['target']!=target: raise Conflict('closure_identity_conflict')
                return cluster['transition_id']
            rows=self.db.execute('SELECT * FROM flow_axioms WHERE owner=? AND cluster_id=? ORDER BY ordinal',(owner,cluster_id)).fetchall()
            if len(rows)!=21 or any(not r['completed'] for r in rows): raise Conflict('cluster_incomplete')
            if self.db.execute('SELECT 1 FROM flow_clusters WHERE owner=? AND id=?',(owner,target)).fetchone(): raise Conflict('target_exists')
            if self.db.execute('SELECT 1 FROM transitions WHERE owner=? AND source=? AND target=?',(owner,cluster_id,target)).fetchone(): raise Conflict('pair_exists')
            tid=str(uuid.uuid4())
            self.db.execute('INSERT INTO transitions VALUES(?,?,?,?,1)',(tid,owner,cluster_id,target))
            self.db.execute('INSERT INTO revisions VALUES(?,?,1,?)',(owner,tid,pack(dict(carry=None,leave=None,open_question=None))))
            self.db.execute('UPDATE flow_clusters SET closed=1,transition_id=?,target=? WHERE owner=? AND id=?',(tid,target,owner,cluster_id))
            return tid
