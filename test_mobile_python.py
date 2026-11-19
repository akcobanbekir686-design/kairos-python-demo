import http.client
import json
import tempfile
import threading
import unittest
from mobile_python import MobileServer
from mobile_demo import Demo
from core import Conflict, Unavailable
from sessions import SessionStore

class MobilePythonTest(unittest.TestCase):
    def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder:
            db = folder+'/demo.db'
            sessions = SessionStore(db)
            token = sessions.issue_session('demo')
            other = sessions.issue_session('other')
            sessions.close()
            with MobileServer(db) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    def call(method,path,body=None,auth=token):
                        conn = http.client.HTTPConnection(*server.server_address,timeout=5)
                        try:
                            conn.request(method,path,json.dumps(body) if body else None,
                                         {'Authorization':'Bearer '+auth,'Content-Type':'application/json'})
                            response = conn.getresponse()
                            return response.status,json.loads(response.read())
                        finally:
                            conn.close()
                    path='/python/readings'
                    first=dict(request_id='first',text='Bir yolda kapıya yaklaştım.',symbols=['Yol','Kapı'],event_time_us=1000000)
                    second=dict(request_id='second',text='Kapının altında su vardı.',symbols=['Kapı','Su'],event_time_us=2000000)
                    self.assertEqual(call('POST',path,first,'invalid')[0],401)
                    code,a=call('POST',path,first)
                    self.assertEqual(code,200,a)
                    self.assertEqual(a['T']['numerator'],0)
                    code,b=call('POST',path,second)
                    self.assertEqual(code,200,b)
                    self.assertEqual(b['T']['ratio'],{'numerator':1,'denominator':2})
                    self.assertEqual(b['selected'][0]['source_id'],a['source_id'])
                    self.assertIn('“Kapı”',b['letter'])
                    self.assertEqual(call('POST',path,second),(200,b))
                    self.assertEqual(call('GET',path+'/second'),(200,b))
                    self.assertEqual(call('GET',path+'/second',auth=other)[0],409)
                    self.assertEqual(call('DELETE','/flow/source/'+a['source_id'])[0],200)
                    self.assertEqual(call('GET',path+'/second'),(409,{'error':'source_unavailable'}))
                    self.assertEqual(call('POST',path,second)[0],409)
                finally:
                    server.shutdown()
                    thread.join()

class BrowserPreviewTest(unittest.TestCase):
    def test_preview_commit_replay_and_erasure(self):
        demo = Demo()
        self.addCleanup(demo.directory.cleanup)
        data = dict(request_id='ignored', text='Ay ışığı.', symbols=['Ay'], event_time_us=1000, scenario=0)
        demo.preview(data)
        first = demo.preview(dict(data, request_id='one', symbols=['Su']))
        self.assertEqual(first['ordinal'], 1)
        self.assertEqual(first['telemetry']['hes'], 68)
        with self.assertRaises(Conflict):
            demo.commit('ignored')
        self.assertEqual(first, demo.commit('one'))
        self.assertEqual(first, demo.commit('one'))
        second = demo.preview(dict(data, request_id='two', symbols=['Su','Kapı'], event_time_us=2000))
        self.assertEqual(second['T']['ratio'], {'numerator':1, 'denominator':2})
        self.assertEqual(second, demo.commit('two'))
        demo.delete(first['source_id'])
        with self.assertRaises(Unavailable):
            demo.read('two')
