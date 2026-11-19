"""Trusted builders re-read under the same SQLite writer fence as delivery."""
import sqlite3

def deliver(handler, store, build, status=200, replay=None):
    started=False
    handler.connection.settimeout(2)
    try:
        with store.atomic():
            body=build()
            started=True
            handler.reply(status,body,replay)
            handler.wfile.flush()
    except OSError:
        handler.close_connection=True
    except sqlite3.Error:
        if not started: raise
        handler.close_connection=True

def beginning_body(store, owner, tid):
    row=store.db.execute('SELECT * FROM beginnings WHERE owner=? AND tid=?',(owner,tid)).fetchone()
    if row is None:
        from core import Unavailable
        raise Unavailable('beginning_unavailable')
    return {'axiom_id':row['axiom'],'transition_id':tid,'araf_version':row['version'],
            'seed_trace':store._read(owner,row['trace'],True),
            'seed_intent':store._read(owner,row['intent'],True) if row['intent'] else None}
