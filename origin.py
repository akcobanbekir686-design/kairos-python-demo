"""Trusted server provenance adapter. Registration is not an HTTP operation.
Text is stored only in units so Core.delete also clears derived text.
"""
import uuid
import json
from core import Core, Conflict, Unavailable, digest, pack

class Origin(Core):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS origin_requests(
          owner TEXT, request_id TEXT, fingerprint TEXT NOT NULL, unit_id TEXT NOT NULL,
          PRIMARY KEY(owner,request_id));
        CREATE TABLE IF NOT EXISTS origin_retired_requests(
          owner TEXT, request_id TEXT, PRIMARY KEY(owner,request_id));
        CREATE TABLE IF NOT EXISTS derivations(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, parents TEXT NOT NULL,
          unit_id TEXT NOT NULL, kind TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS copy_requests(
          owner TEXT, request_id TEXT, source_id TEXT NOT NULL, derivation_id TEXT NOT NULL,
          PRIMARY KEY(owner,request_id));
        CREATE TABLE IF NOT EXISTS transfer_requests(
          owner TEXT, request_id TEXT, tid TEXT NOT NULL, source_version INTEGER NOT NULL,
          target_version INTEGER NOT NULL, operations TEXT NOT NULL,
          PRIMARY KEY(owner,request_id));
        ''')

    def owned_tables(self):
        # Schema discovery prevents future owner-scoped hash tables being silently omitted.
        tables=[]
        for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            name=row[0]
            quoted='"'+name.replace('"','""')+'"'
            if any(col[1]=='owner' for col in self.db.execute('PRAGMA table_info('+quoted+')')):
                tables.append(name)
        return sorted(tables)

    def erase_owner(self, owner):
        """Trusted account lifecycle call. No HTTP/session revocation implemented here.
        Deletes active DB rows including hashes; makes no backup or RAM erasure claim.
        """
        if not isinstance(owner,str) or not owner: raise ValueError('owner')
        self.db.execute('PRAGMA secure_delete=ON')
        with self.atomic():
            for table in self.owned_tables():
                quoted='"'+table.replace('"','""')+'"'
                self.db.execute('DELETE FROM '+quoted+' WHERE owner=?',(owner,))
        # Intentionally no per-user tombstone, hash, or retained erasure receipt.

    def transfer_araf(self, owner, tid, expected_version, request_id):
        if type(expected_version) is not int or expected_version<1: raise ValueError('version')
        if any(not isinstance(x,str) or not x for x in (tid,request_id)): raise ValueError('identity')
        with self.atomic():
            t=self._transition(owner,tid)
            old=self.db.execute('SELECT * FROM transfer_requests WHERE owner=? AND request_id=?',
                                (owner,request_id)).fetchone()
            if old:
                if (old['tid'],old['source_version'])!=(tid,expected_version):
                    raise Conflict('request_payload_conflict')
                self._revision(owner,tid,old['target_version'],True)
                return True, {'transition_id':tid,'version':old['target_version'],
                              'derivations':json.loads(old['operations'])}
            if t['version']!=expected_version: raise Conflict('stale_version')
            self._revision(owner,tid,expected_version,True)
            raw=self.db.execute('SELECT units FROM revisions WHERE owner=? AND tid=? AND version=?',
                                (owner,tid,expected_version)).fetchone()['units']
            fields=json.loads(raw); operations={}
            for key,uid in fields.items():
                operations[key]=None
                if uid is not None:
                    # Transfer is an alias to the same immutable field, not a new source.
                    # Record the operation but never add a self-dependency edge.
                    op=str(uuid.uuid4())
                    self.db.execute('INSERT INTO derivations VALUES(?,?,?,?,?,0)',
                                    (op,owner,pack([uid]),uid,'version_transfer'))
                    operations[key]=op
            version=expected_version+1
            self.db.execute('INSERT INTO revisions VALUES(?,?,?,?)',(owner,tid,version,raw))
            self.db.execute('UPDATE transitions SET version=? WHERE id=? AND owner=?',(version,tid,owner))
            self.db.execute('INSERT INTO transfer_requests VALUES(?,?,?,?,?,?)',
                            (owner,request_id,tid,expected_version,version,pack(operations)))
            return False, {'transition_id':tid,'version':version,'derivations':operations}

    def register_derivation(self, owner, parents, text, kind):
        # Called only by trusted copy/transfer/model producers. No client body accepted.
        if kind not in ('system_copy','version_transfer','model_output'): raise ValueError('kind')
        parents=self.validate_parents(parents)
        with self.atomic():
            return self._register_derivation(owner,parents,text,kind)

    def _register_derivation(self, owner, parents, text, kind):
        # Caller owns transaction; used by both trusted producers.
        op, uid = str(uuid.uuid4()), 'derived:'+uuid.uuid4().hex
        self._add(owner,uid,text,kind,parents)
        self.db.execute('INSERT INTO derivations VALUES(?,?,?,?,?,0)',(op,owner,pack(parents),uid,kind))
        return op

    def copy_source(self, owner, request_id, source_id):
        if any(not isinstance(x,str) or not x for x in (request_id,source_id)):
            raise ValueError('identity')
        with self.atomic():
            old=self.db.execute('SELECT * FROM copy_requests WHERE owner=? AND request_id=?',
                                (owner,request_id)).fetchone()
            if old:
                if old['source_id']!=source_id: raise Conflict('request_payload_conflict')
                op=old['derivation_id']
                target=self.db.execute('SELECT unit_id FROM derivations WHERE id=? AND owner=?',
                                       (op,owner)).fetchone()
                if target is None: raise Unavailable('source_unavailable')
                self._read(owner,target['unit_id'],True)
            else:
                # Read and registration share the write transaction: no stale copied body.
                try: text=self._read(owner,source_id,True)
                except Unavailable: raise Unavailable('source_unavailable') from None
                op=self._register_derivation(owner,[source_id],text,'system_copy')
                self.db.execute('INSERT INTO copy_requests VALUES(?,?,?,?)',(owner,request_id,source_id,op))
            return bool(old), {'derivation_id':op,'derived_from':[source_id]}

    @staticmethod
    def validate_parents(parents):
        if not isinstance(parents,list) or not parents or any(not isinstance(x,str) or not x for x in parents):
            raise ValueError('parents')
        if len(set(parents))!=len(parents): raise ValueError('duplicate_parents')
        return sorted(parents)

    def user_submission(self, owner, request_id, text):
        if not isinstance(request_id,str) or not request_id or not isinstance(text,str) or not text.strip():
            raise ValueError('input')
        fingerprint=digest(text)
        with self.atomic():
            if self.db.execute('SELECT 1 FROM origin_retired_requests WHERE owner=? AND request_id=?',(owner,request_id)).fetchone():
                raise Conflict('submission_retired')
            old=self.db.execute('SELECT * FROM origin_requests WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint: raise Conflict('request_payload_conflict')
                uid=old['unit_id']
                self._read(owner,uid,True)
            else:
                uid='user:'+uuid.uuid4().hex
                self._add(owner,uid,text,'source',[])
                self.db.execute('INSERT INTO origin_requests VALUES(?,?,?,?)',(owner,request_id,fingerprint,uid))
            return bool(old), {'source_id':uid,'origin':'user_submitted'}

    def resolve_submission(self, owner, request_id):
        """Metadata only. Atomically retire an absent attempt so a late POST cannot create it."""
        if not isinstance(request_id,str) or not request_id.strip(): raise ValueError('identity')
        with self.atomic():
            row=self.db.execute('SELECT unit_id FROM origin_requests WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if row:
                try: self._read(owner,row['unit_id'],True)
                except Unavailable: return {'state':'unavailable'}
                return {'state':'stored','source_id':row['unit_id']}
            self.db.execute('INSERT OR IGNORE INTO origin_retired_requests VALUES(?,?)',(owner,request_id))
            return {'state':'not_stored'}

    def consume_derivation(self, owner, derivation_id, parents):
        parents=self.validate_parents(parents)
        if not isinstance(derivation_id,str) or not derivation_id: raise ValueError('operation')
        with self.atomic():
            op=self.db.execute('SELECT * FROM derivations WHERE id=? AND owner=?',(derivation_id,owner)).fetchone()
            if op is None: raise Conflict('derivation_unavailable')
            if op['parents']!=pack(parents): raise Conflict('derivation_binding_conflict')
            self._read(owner,op['unit_id'],True)
            self.db.execute('UPDATE derivations SET consumed=1 WHERE id=?',(derivation_id,))
            return bool(op['consumed']), {'source_id':op['unit_id'],'origin':op['kind']}
