"""Short-lived, isolated browser demos using the same v0.16 reading coordinator."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from contextlib import closing
from tempfile import TemporaryDirectory

from core import Conflict, Unavailable, digest
from mobile_python import create_reading, read_pair
from response_store import ResponseStore
from sessions import SessionStore, Unauthorized

SCENARIOS = (
    (42, 68, [54, 62, 66, 71, 73, 77, 73]),
    (62, 66, [60, 64, 62, 66, 65, 67, 64]),
    (84, 32, [58, 51, 49, 43, 39, 36, 32]),
)


def telemetry(index):
    if type(index) is not int or not 0 <= index < len(SCENARIOS):
        raise ValueError('scenario')
    x, y, days = SCENARIOS[index]
    gap = abs(y-x)
    return dict(mode='simulation', x=x, y=y, days=days, gap=gap,
                hes=round(sum(days)/7), delta=(y-x)/100,
                state='Optimal çizgi' if gap <= 10 else 'Kırılma hattı' if gap >= 35 else 'Geçiş bölgesi')


def present(store, key, index):
    result = read_pair(store, key)
    t = result['T']
    repeated = bool(t['numerator'])
    result.update(
        telemetry=telemetry(index),
        dependencyIds=[row['source_id'] for row in
                       json.loads(store._read(store.request_owner, result['snapshot_id']))['memory']['candidates']],
        status='Radar temsili bir senaryoyu gösteriyor. Sembol karşılaştırması bu oturumdaki izlerine dayanıyor.',
        suggestion=dict(label='Küçük bir adım',
                        reason='Tekrarlanan bir sembol, iki anı karşılaştırman için bir başlangıç sunuyor.' if repeated else
                               'Bugüne ait kısa bir not, bu ana döndüğünde ne hissettiğini hatırlamana yardımcı olabilir.',
                        action='İstersen bu kez farklı hissettiren bir ayrıntıyı yaz.' if repeated else
                               'İstersen aklında kalan bir ayrıntıyı kendi sözcüklerinle yaz.'),
        seal='Ayna yolu seçmez, bakışına yer açar.')
    return result


class Demo:
    def __init__(self):
        self.directory = TemporaryDirectory(prefix='kairos-tab-')
        self.base = Path(self.directory.name) / 'active.sqlite'
        self.stage = self.base.with_name('preview.sqlite')
        self.expires = time.monotonic() + 3600
        self.lock = threading.Lock()
        self.pending = None
        self.saved = {}
        with closing(SessionStore(self.base)) as sessions:
            self.token = sessions.issue_session('tab')

    def preview(self, data):
        index = data['scenario']
        telemetry(index)
        rid = data['request_id']
        if not isinstance(rid, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', rid):
            raise ValueError('request_id')
        fingerprint = digest(data)
        if rid in self.saved:
            if self.saved[rid] != (fingerprint, index):
                raise Conflict('request_changed')
            return self.read(rid)
        if self.pending != (rid, fingerprint, index):
            self.pending = None
            # The session lock covers every database handle; both files are closed here.
            shutil.copyfile(self.base, self.stage)
            with closing(ResponseStore(self.stage, self.token)) as store:
                create_reading(store, data)
            self.pending = (rid, fingerprint, index)
        return self.result(self.stage, rid, index)

    def result(self, path, rid, index):
        with closing(ResponseStore(path, self.token)) as store, store.atomic():
            return present(store, 'mobile/'+rid, index)

    def read(self, rid):
        if rid not in self.saved:
            raise Unavailable('unknown_reading')
        return self.result(self.base, rid, self.saved[rid][1])

    def commit(self, rid):
        if rid not in self.saved:
            if not self.pending or self.pending[0] != rid:
                raise Conflict('preview_changed')
            _, fingerprint, index = self.pending
            os.replace(self.stage, self.base)
            self.saved[rid] = (fingerprint, index)
            self.pending = None
        return self.read(rid)

    def delete(self, source):
        with closing(ResponseStore(self.base, self.token)) as store:
            result = store.delete(store.request_owner, source)
        self.pending = None
        self.stage.unlink(missing_ok=True)
        return result


class DemoSessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        threading.Thread(target=self.cleanup, daemon=True).start()

    def cleanup(self):
        while True:
            time.sleep(30)
            with self.lock:
                for key, demo in list(self.items.items()):
                    if demo.expires <= time.monotonic():
                        self.remove(key)

    def remove(self, key):
        with self.lock:
            demo = self.items.pop(key, None)
            if demo:
                with demo.lock:
                    demo.directory.cleanup()

    def create(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            if len(self.items) >= 64:
                raise Conflict('demo_capacity')
            self.items[self.key(token)] = Demo()
        return dict(token=token)

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode()).digest()

    def handle(self, handler):
        method, path = handler.command, handler.path
        handler.connection.settimeout(6)
        try:
            if (method, path) == ('POST', '/demo/session'):
                handler.body(())
                handler.reply(201, self.create())
                return
            key = self.key(handler.headers.get('Authorization', '').removeprefix('Bearer '))
            with self.lock:
                demo = self.items.get(key)
                if not demo or demo.expires <= time.monotonic():
                    raise Unauthorized()
                if (method, path) == ('DELETE', '/demo/session'):
                    self.remove(key)
                    handler.reply(200, {'cleared': True})
                    return
                demo.lock.acquire()
            try:
                rid = re.fullmatch(r'/demo/readings/([a-zA-Z0-9_-]{1,64})', path)
                src = re.fullmatch(r'/demo/source/(user:[0-9a-f]{32})', path)
                if (method, path) == ('POST', '/demo/preview'):
                    value = demo.preview(handler.body(('request_id','event_time_us','text','symbols','scenario')))
                elif rid and method == 'POST':
                    handler.body(())
                    value = demo.commit(rid[1])
                elif rid and method == 'GET':
                    value = demo.read(rid[1])
                elif src and method == 'DELETE':
                    value = demo.delete(src[1])
                else:
                    handler.reply(404, {'error':'not_found'})
                    return
                handler.reply(200, value)
            finally:
                demo.lock.release()
        except Unauthorized:
            handler.reply(401, {'error':'session_expired'})
        except Conflict as exc:
            handler.reply(429 if exc.code == 'demo_capacity' else 409, {'error':exc.code})
        except Unavailable:
            handler.reply(409, {'error':'source_unavailable'})
        except (ValueError, KeyError, TypeError):
            handler.reply(400, {'error':'invalid_request'})
