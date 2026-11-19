"""Araf/dependency reference core. Not connected to ReadingFlow or the web UI.

Host supplies authenticated user, completed-cluster fact and explicit provenance.
No automatic similarity-based provenance inference. Logical deletion only.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager

class Conflict(ValueError):
    def __init__(self, code, existing_id=None):
        super().__init__(code)
        self.code, self.existing_id = code, existing_id

class Unavailable(ValueError): pass

def pack(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',',':'))
def digest(x): return hashlib.sha256(pack(x).encode()).hexdigest()

class Core:
    def __init__(self, path):
        self.db=sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS units(
          owner TEXT, id TEXT, kind TEXT NOT NULL, body TEXT,
          state TEXT NOT NULL DEFAULT 'active', PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS edges(
          owner TEXT, child TEXT, parent TEXT, PRIMARY KEY(owner,child,parent));
        CREATE INDEX IF NOT EXISTS parent_lookup ON edges(owner,parent);
        CREATE TABLE IF NOT EXISTS transitions(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL,
          target TEXT NOT NULL, version INTEGER NOT NULL,
          UNIQUE(owner,source,target));
        CREATE TABLE IF NOT EXISTS revisions(
          owner TEXT, tid TEXT, version INTEGER, units TEXT NOT NULL,
          PRIMARY KEY(owner,tid,version));
        CREATE TABLE IF NOT EXISTS requests(
          owner TEXT, id TEXT, fingerprint TEXT NOT NULL, tid TEXT NOT NULL,
          version INTEGER NOT NULL, PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS beginnings(
          owner TEXT, tid TEXT, axiom TEXT, version INTEGER NOT NULL,
          trace TEXT NOT NULL, intent TEXT, PRIMARY KEY(owner,tid), UNIQUE(owner,axiom));
        ''')
    def close(self): self.db.close()
    @contextmanager
    def atomic(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
    def _unit(self, owner, uid):
        row=self.db.execute('SELECT * FROM units WHERE owner=? AND id=?',(owner,uid)).fetchone()
        if row is None: raise Unavailable('unknown_source')
        return row
    def _ancestors(self, owner, uid):
        return [r[0] for r in self.db.execute('''WITH RECURSIVE chain(id) AS (
          SELECT ? UNION SELECT e.parent FROM edges e JOIN chain c ON e.child=c.id WHERE e.owner=?)
          SELECT id FROM chain''',(uid,owner))]
    def _permissions(self, owner, uid):
        states=[]
        for ancestor in self._ancestors(owner,uid):
            states.append(self._unit(owner,ancestor)['state'])
        reason='deleted' if 'deleted' in states else 'correction_pending' if 'pending' in states else None
        return {'displayable':reason!='deleted',
                'usable_for_generation':reason is None,'reason':reason}
    def _read(self, owner, uid, generation=False):
        permission=self._permissions(owner,uid)
        if not permission['usable_for_generation' if generation else 'displayable']:
            raise Unavailable('source_unavailable')
        return self._unit(owner,uid)['body']
    def read(self, owner, uid, for_generation=False):
        # Access and ancestry checked in the same transaction as the read.
        with self.atomic(): return self._read(owner,uid,for_generation)
    def _add(self, owner, uid, body, kind, parents):
        if not owner or not uid or not isinstance(body,str) or not body.strip():
            raise ValueError('nonempty_identity_and_body_required')
        if self.db.execute('SELECT 1 FROM units WHERE owner=? AND id=?',(owner,uid)).fetchone():
            raise Conflict('immutable_unit_exists')
        for p in set(parents): self._read(owner,p,True)
        self.db.execute('INSERT INTO units(owner,id,kind,body) VALUES(?,?,?,?)',(owner,uid,kind,body))
        self.db.executemany('INSERT INTO edges VALUES(?,?,?)',[(owner,uid,p) for p in sorted(set(parents))])
    def source(self, owner, uid, body):
        with self.atomic(): self._add(owner,uid,body,'source',[])
    def paragraph(self, owner, uid, body, parents):
        if not parents: raise ValueError('explicit_dependencies_required')
        with self.atomic(): self._add(owner,uid,body,'paragraph',parents)
    def pending(self, owner, uid):
        with self.atomic():
            row=self._unit(owner,uid)
            if row['state']=='deleted': raise Unavailable('deleted')
            self.db.execute("UPDATE units SET state='pending' WHERE owner=? AND id=?",(owner,uid))
    def delete(self, owner, uid):
        with self.atomic():
            self._unit(owner,uid)
            rows=self.db.execute('''WITH RECURSIVE chain(id) AS (
              SELECT ? UNION SELECT e.child FROM edges e JOIN chain c ON e.parent=c.id WHERE e.owner=?)
              SELECT u.id,u.kind,u.state FROM units u JOIN chain c ON u.id=c.id WHERE u.owner=?''',
              (uid,owner,owner)).fetchall()
            direct={r[0] for r in self.db.execute('SELECT child FROM edges WHERE owner=? AND parent=?',(owner,uid))}
            newly={r['id'] for r in rows if r['kind']=='paragraph' and r['state']!='deleted' and r['id']!=uid}
            self.db.executemany("UPDATE units SET state='deleted',body=NULL WHERE owner=? AND id=?",
                                [(owner,r['id']) for r in rows])
            return {'direct':len(newly & direct),'indirect':len(newly-direct),'total':len(newly)}
    def _transition(self, owner, tid):
        row=self.db.execute('SELECT * FROM transitions WHERE id=? AND owner=?',(tid,owner)).fetchone()
        if row is None: raise Unavailable('unknown_transition')
        return row
    def revision(self, owner, tid, version, *, for_generation=False):
        with self.atomic():
            self._transition(owner,tid)
            return self._revision(owner,tid,version,for_generation)
    def _revision(self, owner, tid, version, for_generation=False):
        row=self.db.execute('SELECT units FROM revisions WHERE owner=? AND tid=? AND version=?',
                            (owner,tid,version)).fetchone()
        if row is None: raise Unavailable('unknown_revision')
        fields={}; statuses={}; permissions={}
        for key,uid in json.loads(row['units']).items():
            if uid is None:
                fields[key]=None; statuses[key]='empty'
                # Visibility of the empty field, not permission to invent content.
                permissions[key]={'displayable':True,'usable_for_generation':False,'reason':'empty'}
            else:
                permissions[key]=self._permissions(owner,uid)
                if for_generation and not permissions[key]['usable_for_generation']:
                    raise Unavailable('source_unavailable')
                try:
                    fields[key]=self._read(owner,uid)
                    statuses[key]=permissions[key]['reason'] or 'available'
                except Unavailable:
                    fields[key]=None; statuses[key]='unavailable'
        # Availability after deletion does not rewrite the historical choice.
        original_nonempty=any(v is not None for v in json.loads(row['units']).values())
        return {'transition_id':tid,'version':version,'fields':fields,'field_status':statuses,
                'field_permissions':permissions,
                'selection_status':'selected' if original_nonempty else 'none_selected'}
    def revision_count(self, owner, tid):
        return self.db.execute('SELECT count(*) FROM revisions WHERE owner=? AND tid=?',(owner,tid)).fetchone()[0]
    def save(self, owner, tid, source, target, request_id, expected_version, fields,
             *, source_completed, dependencies=None):
        parsed=uuid.UUID(tid)
        if parsed.version!=4 or str(parsed)!=tid: raise ValueError('canonical_uuid4_required')
        if not owner or not source or not target or source==target or not request_id:
            raise ValueError('identities_required')
        if type(expected_version) is not int or expected_version<0: raise ValueError('invalid_version')
        if type(source_completed) is not bool: raise ValueError('completion_boolean_required')
        keys=('carry','leave','open_question')
        if not isinstance(fields,dict) or set(fields)-set(keys): raise ValueError('unknown_fields')
        normalized={k:fields.get(k) for k in keys}
        for k,v in normalized.items():
            if v is not None and (not isinstance(v,str) or not v.strip()): raise ValueError('use_null_for_empty')
        deps=dependencies or {}
        if set(deps)-set(keys): raise ValueError('unknown_dependency_field')
        for k,parents in deps.items():
            if not isinstance(parents,list) or any(not isinstance(p,str) or not p for p in parents):
                raise ValueError('invalid_dependencies')
            if parents and normalized[k] is None: raise ValueError('dependency_without_content')
        deps={k:sorted(set(deps.get(k,[]))) for k in keys}
        fingerprint=digest([tid,source,target,expected_version,normalized,deps])
        with self.atomic():
            row=self.db.execute('SELECT * FROM requests WHERE owner=? AND id=?',(owner,request_id)).fetchone()
            if row:
                if row['fingerprint']!=fingerprint: raise Conflict('request_payload_conflict')
                return self._revision(owner,row['tid'],row['version'])
            t=self.db.execute('SELECT * FROM transitions WHERE id=?',(tid,)).fetchone()
            if t and (t['owner'],t['source'],t['target'])!=(owner,source,target):
                raise Conflict('transition_identity_conflict')
            if not t:
                old=self.db.execute('SELECT id FROM transitions WHERE owner=? AND source=? AND target=?',
                                    (owner,source,target)).fetchone()
                if old: raise Conflict('pair_exists',old['id'])
                if not source_completed: raise Conflict('source_incomplete')
                if expected_version!=0: raise Conflict('stale_version')
                self.db.execute('INSERT INTO transitions VALUES(?,?,?,?,0)',(tid,owner,source,target))
                version=1
            else:
                if t['version']!=expected_version: raise Conflict('stale_version')
                version=expected_version+1
            previous={}
            if version>1:
                previous=json.loads(self.db.execute(
                    'SELECT units FROM revisions WHERE owner=? AND tid=? AND version=?',
                    (owner,tid,version-1)).fetchone()['units'])
            units={}
            for k,v in normalized.items():
                units[k]=None
                if v is not None:
                    old_uid=previous.get(k)
                    parents=set(deps[k])
                    if old_uid:
                        old_body=self._read(owner,old_uid,True)
                        known=set(self._ancestors(owner,old_uid))
                        if old_body==v and parents<=known:
                            units[k]=old_uid
                            continue
                        # Editing a field is not evidence that its known origin vanished.
                        parents.add(old_uid)
                    uid='araf:'+uuid.uuid4().hex
                    self._add(owner,uid,v,'araf_field',parents); units[k]=uid
            self.db.execute('INSERT INTO revisions VALUES(?,?,?,?)',(owner,tid,version,pack(units)))
            self.db.execute('UPDATE transitions SET version=? WHERE id=?',(version,tid))
            self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(owner,request_id,fingerprint,tid,version))
            return self._revision(owner,tid,version)
    def begin(self, owner, tid, axiom, trace_source, intent_source=None, *, expected_araf_version, with_receipt=False):
        if type(with_receipt) is not bool: raise ValueError('receipt_boolean_required')
        if type(expected_araf_version) is not int or expected_araf_version<1:
            raise ValueError('positive_expected_araf_version_required')
        with self.atomic():
            return self._begin(owner,tid,axiom,trace_source,intent_source,expected_araf_version=expected_araf_version,with_receipt=with_receipt)

    def _begin(self, owner, tid, axiom, trace_source, intent_source=None, *, expected_araf_version, with_receipt=False):
        """Internal: caller must own the write transaction."""
        t=self._transition(owner,tid)
        if not axiom: raise ValueError('axiom_id_required')
        for uid in [trace_source,intent_source]:
            if uid is not None:
                if self._unit(owner,uid)['kind']!='source': raise Conflict('user_input_required')
                self._read(owner,uid,True)
        if trace_source is None: raise Unavailable('real_axiom_required')
        b=self.db.execute('SELECT * FROM beginnings WHERE owner=? AND tid=?',(owner,tid)).fetchone()
        if b:
            if (b['axiom'],b['trace'],b['intent'])!=(axiom,trace_source,intent_source):
                raise Conflict('beginning_identity_conflict')
            if b['version']!=expected_araf_version:
                raise Conflict('beginning_identity_conflict')
            version=b['version']
        else:
            if t['version']!=expected_araf_version:
                raise Conflict('araf_version_conflict')
            if self.db.execute('SELECT 1 FROM beginnings WHERE owner=? AND axiom=?',(owner,axiom)).fetchone():
                raise Conflict('axiom_already_used')
            version=t['version']
            self.db.execute('INSERT INTO beginnings VALUES(?,?,?,?,?,?)',
                            (owner,tid,axiom,version,trace_source,intent_source))
        result={'axiom_id':axiom,'transition_id':tid,'araf_version':version,
                'seed_trace':self._read(owner,trace_source,True),
                'seed_intent':self._read(owner,intent_source,True) if intent_source else None}
        # Computed under the same write transaction; no racy HTTP-side existence query.
        return {'replay':b is not None,'result':result} if with_receipt else result
