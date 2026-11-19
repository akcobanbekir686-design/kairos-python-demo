"""Trusted producer adapter. No public generation endpoint or live provider.
All context records are conservative dependencies; provider cannot declare parents.
"""
from dataclasses import dataclass
import json
from core import Conflict, Unavailable, pack
from sessions import AuthorizedStore
from providers import ProviderError

@dataclass(frozen=True)
class ContextRecord:
    source_id: str
    text: str

class ModelStore(AuthorizedStore):
    def __init__(self,path,token):
        super().__init__(path,token)
        self.db.execute('''CREATE TABLE IF NOT EXISTS model_jobs(
          owner TEXT NOT NULL, request_id TEXT NOT NULL, parents TEXT NOT NULL,
          provider_version TEXT NOT NULL, output_id TEXT, derivation_id TEXT,
          PRIMARY KEY(owner,request_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS model_attempts(
          owner TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
          attempt INTEGER NOT NULL, PRIMARY KEY(owner,request_id))''')

    def generate(self,request_id,source_ids,provider,*,provider_version,single_dispatch=False):
        """Only trusted orchestration calls this; provider has no DB credential.
        Caller must include ALL user-specific context in source_ids, no hidden memory.
        Returns identifiers, never a potentially stale generated body.
        """
        if any(not isinstance(v,str) or not v.strip() for v in (request_id,provider_version)):
            raise ValueError('identity')
        parents=self.validate_parents(source_ids)
        owner=self.request_owner
        attempt=None
        with self.atomic():
            self._check_generation_fence(owner,request_id)
            job=self.db.execute('SELECT * FROM model_jobs WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if job and (job['parents'],job['provider_version'])!=(pack(parents),provider_version):
                raise Conflict('request_payload_conflict')
            if job and job['output_id']:
                return self._replay(owner,job)
            context=tuple(ContextRecord(uid,self._read(owner,uid,True)) for uid in parents)
            if not job:
                self.db.execute('INSERT INTO model_jobs VALUES(?,?,?,?,NULL,NULL)',
                                (owner,request_id,pack(parents),provider_version))
            if single_dispatch:
                claim=self.db.execute('SELECT * FROM model_attempts WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
                # Old unfinished v0.3.3 jobs have no evidence that dispatch never happened.
                if claim is None and job is not None:
                    raise Conflict('generation_outcome_unknown')
                if claim and claim['state']!='retryable':
                    raise Conflict('generation_outcome_unknown' if claim['state']=='uncertain' else 'generation_already_dispatched')
                attempt=claim['attempt']+1 if claim else 1
                self.db.execute("INSERT INTO model_attempts VALUES(?,?,'dispatched',?) ON CONFLICT(owner,request_id) DO UPDATE SET state='dispatched',attempt=excluded.attempt",(owner,request_id,attempt))
        # Claim commits BEFORE sending. Never hold the SQLite writer during I/O.
        try:
            text=provider(context)
            if not isinstance(text,str) or not text.strip(): raise ValueError('provider_text_required')
        except BaseException as exc:
            if attempt is not None:
                # Only explicit pre-send failures / definitive 429 may be retried.
                state='retryable' if isinstance(exc,ProviderError) and exc.retry_safe else 'uncertain'
                with self.atomic():
                    self.db.execute('UPDATE model_attempts SET state=? WHERE owner=? AND request_id=? AND attempt=? AND state="dispatched"',(state,owner,request_id,attempt))
            raise
        with self.atomic():
            self._check_generation_fence(owner,request_id)
            job=self.db.execute('SELECT * FROM model_jobs WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if job is None: raise Unavailable('generation_unavailable')
            if (job['parents'],job['provider_version'])!=(pack(parents),provider_version):
                raise Conflict('request_payload_conflict')
            if job['output_id']: return self._replay(owner,job)
            # Parent bodies are immutable; pending/deleted ancestry blocks _add.
            op=self._register_derivation(owner,parents,text,'model_output')
            uid=self.db.execute('SELECT unit_id FROM derivations WHERE id=?',(op,)).fetchone()[0]
            self.db.execute('UPDATE model_jobs SET output_id=?,derivation_id=? WHERE owner=? AND request_id=?',
                            (uid,op,owner,request_id))
            if attempt is not None:
                self.db.execute("UPDATE model_attempts SET state='succeeded' WHERE owner=? AND request_id=? AND attempt=?",(owner,request_id,attempt))
            return False, {'source_id':uid,'derivation_id':op,'origin':'model_output'}

    def _check_generation_fence(self,owner,request_id):
        row=self.db.execute('SELECT state FROM model_attempts WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
        if row and row[0]=='superseded':raise Conflict('generation_superseded')

    def _replay(self,owner,job):
        self._read(owner,job['output_id'],True)
        return True, {'source_id':job['output_id'],'derivation_id':job['derivation_id'],'origin':'model_output'}
