"""Isolated 5-tester budget and results. All spend decisions share a DB lock."""
import hashlib
import json
import time
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from beta_reading import PERSONA, history_provenance, model_context, payload_for, request_data, reservation, usage_cost, validate
from providers import ProviderError

DDL=[
    'CREATE TABLE IF NOT EXISTS beta_meter_v1 (owner INTEGER NOT NULL, id TEXT NOT NULL, fingerprint TEXT NOT NULL, month TEXT NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL, model TEXT NOT NULL, persona TEXT NOT NULL, charge BIGINT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, PRIMARY KEY(owner,id))',
    'CREATE TABLE IF NOT EXISTS beta_letters_v1 (owner INTEGER NOT NULL,id TEXT NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(owner,id),UNIQUE(owner,ordinal))',
    'CREATE TABLE IF NOT EXISTS beta_history_v1 (owner INTEGER NOT NULL,id TEXT NOT NULL,ordinal INTEGER NOT NULL,text_body TEXT NOT NULL,symbols TEXT NOT NULL, PRIMARY KEY(owner,id))'
]

class Ledger:
    def __init__(self,connect,*,sqlite=False):
        self.connect=connect
        self.sqlite=sqlite  # Local isolated tests only. The network entry uses PostgreSQL.

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db:
            cur=db.cursor()
            cur.execute('BEGIN IMMEDIATE' if self.sqlite else 'SELECT pg_advisory_xact_lock(267712061)')
            def sql(statement,args=()):
                cur.execute(statement if self.sqlite else statement.replace('?','%s'),args)
                return cur
            try:
                yield sql
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self):
        with self.transaction() as sql:
            for statement in DDL: sql(statement)

    def receipts(self,owner):
        with self.transaction() as sql:
            rows=sql('SELECT id,ordinal FROM beta_letters_v1 WHERE owner=? ORDER BY ordinal DESC',(owner,)).fetchall()
            return [dict(id=row[0],ordinal=row[1]) for row in rows]

    def receipt(self,owner,rid):
        with self.transaction() as sql:
            row=sql('SELECT state FROM beta_meter_v1 WHERE owner=? AND id=?',(owner,rid)).fetchone()
            if not row: raise ProviderError('unknown_request',404)
            if row[0]!='done': raise ProviderError('already_submitted' if row[0]=='active' else row[0],409)
            return json.loads(sql('SELECT body FROM beta_letters_v1 WHERE owner=? AND id=?',(owner,rid)).fetchone()[0])

    def budget_summary(self,cap):
        with self.transaction() as sql:
            used,held,calls=sql("""SELECT
                COALESCE(SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN charge ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN charge ELSE 0 END),0),
                COUNT(*) FROM beta_meter_v1""").fetchone()
        return dict(metered=int(used),reserved=int(held),limit=cap,remaining=cap-int(used)-int(held),calls=calls,
                    day_per_user=3,month_per_user=21,simultaneous=1)

    def submit(self,owner,body,provider,cap,*,now=None,paused=False):
        data=request_data(body)
        if not 1<=owner<=5: raise ProviderError('beta_only',403)
        rid=data['id']
        finger=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        stamp=datetime.fromtimestamp(time.time() if now is None else now,timezone.utc).date().isoformat()
        with self.transaction() as sql:
            old=sql('SELECT fingerprint,state FROM beta_meter_v1 WHERE owner=? AND id=?',(owner,rid)).fetchone()
            if old:
                if old[0]!=finger: raise ProviderError('changed_request',409)
                if old[1]=='done':
                    return json.loads(sql('SELECT body FROM beta_letters_v1 WHERE owner=? AND id=?',(owner,rid)).fetchone()[0])
                raise ProviderError(old[1] if old[1]!='active' else 'already_submitted',409)
            if paused: raise ProviderError('review_pause',403)
            if cap<=0: raise ProviderError('budget_closed',403)
            if sql("SELECT COUNT(*) FROM beta_meter_v1 WHERE state='active'").fetchone()[0]: raise ProviderError('busy',429)
            count=lambda query,params:sql(query,params).fetchone()[0]
            if count('SELECT COUNT(*) FROM beta_meter_v1 WHERE owner=? AND day=?',(owner,stamp))>=3 or count('SELECT COUNT(*) FROM beta_meter_v1 WHERE owner=? AND month=?',(owner,stamp[:7]))>=21:
                raise ProviderError('tester_quota',429)
            ordinal=1+count('SELECT COUNT(*) FROM beta_letters_v1 WHERE owner=?',(owner,))
            if ordinal>21: raise ProviderError('cluster_complete',409)
            history=[]
            if data['history']:
                rows=sql('SELECT id,text_body,symbols FROM beta_history_v1 WHERE owner=? ORDER BY ordinal DESC',(owner,)).fetchall()
                history=[dict(id=r[0],text=r[1],symbols=json.loads(r[2])) for r in rows]
            ctx,console=model_context(owner,data,history,ordinal)
            payload=payload_for(provider.model,ctx)
            hold=reservation(payload)
            # Campaign lifetime cap also bounds every calendar month, including restarts.
            if hold+count('SELECT COALESCE(SUM(charge),0) FROM beta_meter_v1',())>cap:
                raise ProviderError('budget_exhausted',429)
            sql('INSERT INTO beta_meter_v1(owner,id,fingerprint,month,day,state,model,persona,charge) VALUES(?,?,?,?,?,?,?,?,?)',
                (owner,rid,finger,stamp[:7],stamp,'active',provider.model,PERSONA,hold))
        used=None
        try:
            response=provider.request_payload(payload)
            used=usage_cost(response,provider.model)
            if used is None or used[2]>hold: raise ProviderError('usage_unconfirmed')
            parsed=validate(provider.output_text(response,structured=True),ctx)
        except Exception as exc:
            err=exc.code if isinstance(exc,ProviderError) else 'provider_uncertain'
            if err=='invalid_letter' and hasattr(exc,'rule'):
                print('KAIROS_LETTER_CHECK '+exc.rule,flush=True)
            with self.transaction() as sql:
                sql('UPDATE beta_meter_v1 SET state=?,charge=?,input_tokens=?,output_tokens=? WHERE owner=? AND id=?',
                    (err,used[2] if used else hold,used[0] if used else None,used[1] if used else None,owner,rid))
            raise ProviderError(err,getattr(exc,'status',502)) from None
        provenance=console['provenance']
        provenance['inferred'].extend(parsed['inferences'])
        if data['history']:
            previous={g['symbol'] for row in history for g in history_provenance(row)['inferred']}
            names=[g['symbol'] for g in provenance['inferred']]
            matched=[s for s in names if s in previous]
            console['repetition']['inferred']=dict(matched=matched,ratio=[len(matched),len(names)])
        result=dict(console=console,letter=parsed['letter'],observation=parsed['observation'],seal=parsed['seal'],ordinal=ordinal)
        with self.transaction() as sql:
            sql('INSERT INTO beta_letters_v1 VALUES(?,?,?,?)',(owner,rid,ordinal,json.dumps(result,ensure_ascii=False)))
            if data['remember']:
                sql('INSERT INTO beta_history_v1 VALUES(?,?,?,?,?)',(owner,rid,ordinal,data['text'],json.dumps(provenance,ensure_ascii=False)))
            sql("UPDATE beta_meter_v1 SET state='done',charge=?,input_tokens=?,output_tokens=? WHERE owner=? AND id=?",
                (used[2],used[0],used[1],owner,rid))
        return result
