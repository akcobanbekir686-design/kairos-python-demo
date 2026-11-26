import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from beta_reading import MODEL,OUTPUT,payload_for,validate
from beta_server import readiness
from beta_store import Ledger
from providers import OpenAIResponsesProvider,ProviderError

class BetaTests(unittest.TestCase):
    def setUp(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        path=Path(d.name)/'isolated.sqlite'
        self.db=Ledger(lambda:sqlite3.connect(path),sqlite=True);self.db.initialize()
        self.ins=[];self.hook=None
        def transport(payload,key,timeout):
            c=json.loads(payload['input']);self.ins.append(c)
            if self.hook: self.hook()
            body=dict(letter='Bir eşikte durduğunu yazmışsın. Bu ayrıntıyı kendi hızında düşünebilirsin.',observation=None,
                      seal='Bu küme burada kapanır.' if c['ordinal']==21 else None,sources=[r['id'] for r in c['history']])
            return dict(status='completed',usage=dict(input_tokens=700,output_tokens=122,input_tokens_details={'cache_write_tokens':0}),
                        output=[dict(type='message',role='assistant',status='completed',content=[dict(type='output_text',text=json.dumps(body))])])
        self.provider=OpenAIResponsesProvider(model=MODEL,api_key='local-test-placeholder',max_output_tokens=OUTPUT,transport=transport)
    def data(self,text='Eşiğe yaklaştım.',*,share=True,history=True,remember=False):
        return dict(id=str(uuid.uuid4()),text=text,symbols=['Kapı'],share=share,history=history,remember=remember)
    def runone(self,body,owner=1,cap=3_000_000_000):
        return self.db.submit(owner,body,self.provider,cap)
    def test_three_readings_and_isolation(self):
        first=self.data('Eski gerçek kapı anlatısı',remember=True)
        a=self.runone(first);self.assertEqual([],self.ins[0]['history'])
        second=self.data('Bir kapının önünde uyandım.');b=self.runone(second)
        self.assertEqual(b['console']['ratio'],[1,1]);self.assertEqual(first['text'],self.ins[1]['history'][0]['text'])
        c=self.runone(self.data(history=False));self.assertNotIn('calculated',self.ins[2]);self.assertNotIn(first['text'],json.dumps(self.ins[2],ensure_ascii=False))
        self.assertIsNone(c['console']['ratio']);self.assertEqual(a,self.runone(first));self.assertEqual(len(self.ins),3)
        self.runone(self.data(),owner=2);self.assertEqual(self.ins[-1]['history'],[])
        self.assertIsNone(b['seal']);self.assertNotIn('cosine',b['letter'])
    def test_denied_and_limits(self):
        for b,limit,err in [(self.data(share=False),10**9,'consent_required'),(self.data(),0,'budget_closed'),(self.data(),100,'budget_exhausted')]:
            with self.assertRaisesRegex(ProviderError,err): self.runone(b,cap=limit)
        self.assertFalse(self.ins)
        for _ in range(3): self.runone(self.data())
        with self.assertRaisesRegex(ProviderError,'tester_quota'): self.runone(self.data())
        with self.db.transaction() as sql:
            self.assertEqual(sql('SELECT SUM(input_tokens),SUM(output_tokens),SUM(charge) FROM beta_meter_v1').fetchone(),(2100,366,3*2620000))
    def test_simultaneous(self):
        entered=threading.Event();release=threading.Event()
        self.hook=lambda:(entered.set(),release.wait(3))
        with ThreadPoolExecutor(2) as pool:
            a=self.data();one=pool.submit(self.runone,a);self.assertTrue(entered.wait(2))
            for b,err in [(a,'already_submitted'),(self.data(),'busy')]:
                with self.assertRaisesRegex(ProviderError,err): self.runone(b)
            release.set();one.result()
        self.assertEqual(len(self.ins),1)
    def test_ambiguous_and_incomplete(self):
        self.hook=lambda:(_ for _ in ()).throw(ProviderError('provider_timeout'))
        a=self.data()
        for _ in (1,2):
            with self.assertRaisesRegex(ProviderError,'provider_timeout'): self.runone(a)
        self.assertEqual(len(self.ins),1)
        self.provider._transport=lambda *args:{'status':'incomplete','usage':{'input_tokens':50,'output_tokens':2048}}
        a=self.data()
        with self.assertRaisesRegex(ProviderError,'provider_incomplete'): self.runone(a)
        with self.db.transaction() as sql:
            self.assertEqual(sql('SELECT COUNT(*) FROM beta_letters_v1').fetchone()[0],0)
            self.assertEqual(sql('SELECT output_tokens FROM beta_meter_v1 WHERE id=?',(a['id'],)).fetchone()[0],2048)
    def test_schema_and_seal(self):
        c=dict(ordinal=1,history=[]);v=dict(letter='Sözün burada.',observation=None,seal=None,sources=[])
        for changes in ({'sources':['K1']},{'letter':'X: 45'},{'seal':'Erken mühür.'}):
            with self.assertRaises(ProviderError): validate(json.dumps(v|changes),c)
        self.assertEqual('null',payload_for(MODEL,c)['text']['format']['schema']['properties']['seal']['type'])
        self.assertEqual('Son.',validate(json.dumps(v|{'seal':'Son.'}),c|{'ordinal':21})['seal'])
    def test_global_cap_restart_and_month(self):
        first=self.data();self.runone(first)
        with self.db.transaction() as sql:
            cost=sql('SELECT charge FROM beta_meter_v1').fetchone()[0]
        nextbody=self.data()
        from beta_reading import reservation,model_context
        context,_=model_context(2,nextbody,[],1)
        hold=reservation(payload_for(MODEL,context))
        restarted=Ledger(self.db.connect,sqlite=True)
        with self.assertRaisesRegex(ProviderError,'budget_exhausted'):
            restarted.submit(2,nextbody,self.provider,cost+hold-1)
        self.assertEqual(len(self.ins),1)
        restored=restarted.submit(1,first,self.provider,3_000_000_000)
        self.assertEqual(restored['ordinal'],1)
        for n in range(20):
            restarted.submit(3,self.data(),self.provider,3_000_000_000,now=1772323200+n*86400)
        last=restarted.submit(3,self.data(),self.provider,3_000_000_000,now=1772323200+20*86400)
        self.assertEqual(last['ordinal'],21);self.assertIsNotNone(last['seal'])
        with self.assertRaisesRegex(ProviderError,'tester_quota'):
            restarted.submit(3,self.data(),self.provider,3_000_000_000,now=1772323200+21*86400)

    def test_readiness(self):
        with patch.dict('os.environ',{},clear=True): self.assertEqual(readiness(),'missing_key')
        with patch.dict('os.environ',{'OPENAI_API_KEY':'fixture'},clear=True): self.assertEqual(readiness(),'budget_closed')
