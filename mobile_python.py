"""First-cluster Python mobile integration. Launch on loopback behind a trusted gateway."""
import json
import re
import sqlite3
import threading
from http.server import ThreadingHTTPServer

from core import Conflict, Unavailable
from delivery_gate import deliver
from reading_http import ReadingHandler
from response_store import ResponseStore
from sessions import Unauthorized

SYMBOLS = ('Ay', 'Su', 'Kapı', 'Yol', 'Ağaç', 'Işık', 'Ev', 'Kuş')
VECTOR = dict(model_revision='mobile-symbols-v1', preprocessing_revision='binary-v1')


def letter(context):
    t = context['calculations']['T']
    selected = {row['source_id'] for row in context['history']}
    repeated = list(t.get('evidence', {}))
    match = next((s for s in repeated if selected.intersection(t['evidence'][s])), None)
    if match:
        opening = f'“{match}” bu mektuba eşlik eden eski bir izinde de vardı. O zamanki sahneyi düşün. Bugün aynı sözcüğün yanında ne değişti?'
    elif repeated:
        opening = f'“{repeated[0]}” daha önce seçtiğin bir sembol. Bugünkü anlamını kendi sözlerinle nasıl anlatırsın?'
    elif t['history_coverage']['compatible_records']:
        opening = 'Bugün seçtiğin semboller önceki izlerinde görünmüyor. Bu anın aklında duran ayrıntısı hangisi?'
    else:
        opening = 'Bu ilk izini burada tutuyorum. Anlattığın ana döndüğünde hangi ayrıntıda biraz daha kalmak istersin?'
    return opening + '\n\nİstersen o ayrıntıyı bir cümleyle mektubun yanına bırak. İyi günler.'


def create_reading(store, data):
    rid = data['request_id']
    symbols = data['symbols']
    if not isinstance(rid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', rid):
        raise ValueError('request_id')
    if not isinstance(data['text'], str) or not 0 < len(data['text'].strip()) <= 3000:
        raise ValueError('text')
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= len(SYMBOLS) or any(s not in SYMBOLS for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError('symbols')
    if type(data['event_time_us']) is not int or not 0 < data['event_time_us'] < 2**63:
        raise ValueError('event_time')
    key = 'mobile/' + rid
    owner = store.request_owner
    _, source = store.user_submission(owner, key, data['text'])
    sid = source['source_id']
    with store.atomic():
        session = store.db.execute('SELECT state FROM narrative_sessions WHERE owner=? AND id=?',(owner,key)).fetchone()
    if not session:
        store.open_narrative_session(key)
    store.register_narrative(sid,key,symbols,dictionary_version='mobile-tr-v1')
    store.register_vector(sid,[int(s in symbols) for s in SYMBOLS],**VECTOR)
    store.register_event_time(sid,data['event_time_us'])
    store.append('mobile-first-cluster',key,sid)
    tid = store.calculate_t(key,sid)
    aid = store.calculate_a(key,sid,**VECTOR)
    mid = store.select_memory(key,sid,aid)
    snap = store.prepare_response(key,sid,tid,mid)
    store.generate_response(key,key,snap,letter,provider_version='python-letter-v1')
    store.close_narrative_session(key,'completed')
    return key


def read_pair(store, key):
    # Called within the existing delivery fence, including the final socket flush.
    ax = store._axiom(key)
    owner = store.request_owner
    row = store.db.execute('SELECT result_id FROM response_requests WHERE owner=? AND request_id=? AND source_id=?',
                           (owner,key,ax['source_id'])).fetchone()
    if not ax['completed'] or not row:
        raise Unavailable('reading_incomplete')
    snapshot = row['result_id']
    if snapshot not in store._ancestors(owner, ax['output_id']):
        raise Conflict('snapshot_mismatch')
    bundle = json.loads(store._read(owner,snapshot))
    return dict(source_id=ax['source_id'], snapshot_id=snapshot, ordinal=ax['ordinal'],
                console=bundle['console'], T=bundle['calculations']['T'],
                selected=bundle['memory']['selected'], letter=store._read(owner,ax['output_id']))


class MobileHandler(ReadingHandler):
    def do_POST(self):
        if self.path == '/python/readings':
            return self.mobile(True)
        self.reply(404, {'error':'not_found'})

    def do_GET(self):
        if re.fullmatch(r'/python/readings/[A-Za-z0-9_-]{1,64}',self.path):
            return self.mobile(False)
        self.reply(404, {'error':'not_found'})

    def do_DELETE(self):
        if re.fullmatch(r'/flow/source/user:[0-9a-f]{32}', self.path):
            return super().do_DELETE()
        self.reply(404, {'error':'not_found'})

    def mobile(self, create):
        store = None
        self.connection.settimeout(3)
        try:
            auth = self.headers.get('Authorization','')
            if not auth.startswith('Bearer ') or not 1 <= len(auth[7:]) <= 512:
                raise Unauthorized()
            store = ResponseStore(self.server.db_path,auth[7:])
            if create:
                data = self.body(('request_id','text','symbols','event_time_us'))
                with self.server.create_lock:
                    key = create_reading(store,data)
            else:
                key = 'mobile/' + self.path.rsplit('/',1)[1]
            deliver(self,store,lambda:read_pair(store,key))
        except Unauthorized:
            self.reply(401,{'error':'unauthorized'})
        except Conflict as exc:
            self.reply(409,{'error':exc.code})
        except Unavailable:
            self.reply(409,{'error':'source_unavailable'})
        except (ValueError, TypeError):
            self.reply(400,{'error':'invalid_request'})
        except sqlite3.Error:
            self.reply(503,{'error':'database_unavailable'})
        finally:
            if store is not None:
                store.close()


class MobileServer(ThreadingHTTPServer):
    def __init__(self,path,port=0):
        self.create_lock = threading.Lock()
        self.db_path = path
        super().__init__(('127.0.0.1',port),MobileHandler)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--port',type=int,default=8080)
    args = parser.parse_args()
    with MobileServer(args.db,args.port) as server:
        server.serve_forever()
