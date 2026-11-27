import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from beta_reading import MODEL,OUTPUT,narrative_passages,payload_for,validate
from beta_server import readiness
from beta_store import Ledger
from providers import OpenAIResponsesProvider,ProviderError

class BetaTests(unittest.TestCase):
    def setUp(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        path=Path(d.name)/'isolated.sqlite'
        self.db=Ledger(lambda:sqlite3.connect(path),sqlite=True);self.db.initialize()
        self.ins=[];self.hook=None;self.guesses=[]
        def transport(payload,key,timeout):
            c=json.loads(payload['input']);self.ins.append(c)
            if self.hook: self.hook()
            body=dict(inferences=self.guesses,letter='Bir eşikte durduğunu yazmışsın. Bu ayrıntıyı kendi hızında düşünebilirsin.',observation=None,
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
        c=dict(ordinal=1,history=[]);v=dict(inferences=[],letter='Sözün burada.',observation=None,seal=None,sources=[])
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

    def test_teaching_dreams_tags_are_not_events(self):
        first=self.data('Rüyamda bir sınıfa bir şeyler anlatıp duruyormuşum.',remember=True)
        first.update(symbols=['Kuş'],experience='rüya')
        self.runone(first)
        second=self.data('Rüyamda bir öğrenciye ders anlatıyordum.')
        second.update(symbols=['Kuş'],experience='rüya')
        result=self.runone(second)
        ctx=self.ins[-1]
        self.assertEqual(ctx['symbol_sources'],{'mentioned':[],'tagged':['Kuş']})
        self.assertEqual(ctx['history'][0]['symbol_sources']['mentioned'],[])
        self.assertEqual(ctx['history'][0]['experience'],'rüya')
        self.assertEqual(result['console']['repetition']['tagged']['matched'],['Kuş'])
        self.assertEqual(result['console']['repetition']['mentioned']['matched'],[])
        base=dict(observation=None,seal=None,sources=[],inferences=[])
        for letter in ('Rüyanda ben ders anlattım.','Rüyanda bir kuş gördün.','Benzerlik: 0,82.'):
            with self.assertRaises(ProviderError): validate(json.dumps(base|{'letter':letter}),ctx)
        validate(json.dumps(base|{'letter':'Kuşu iki kez etiket olarak seçmişsin.'}),ctx)

    def test_mention_tag_and_inference_channels(self):
        first=self.data('Kapının önünde durdum. Kuru bir yaprak vardı.',remember=True)
        first.update(symbols=['Kuş'],experience='déjà vu')
        self.guesses=[dict(symbol='Ağaç',passage='D2')]
        old=self.runone(first)
        p=old['console']['provenance']
        self.assertEqual(set(p['mentioned']),{'Kapı'})
        start,end=p['inferred'][0]['basis']
        self.assertEqual(first['text'][start:end],'Kuru bir yaprak vardı.')
        second=self.data('Bir kuşu fark ettim. Yerde kuru bir yaprak vardı.')
        second.update(symbols=['Kapı'],experience='eşzamanlılık')
        self.guesses=[dict(symbol='Ağaç',passage='D2')]
        c=self.runone(second)['console']
        self.assertEqual(c['sources'],[]) # Tag/mention crossover does not raise similarity.
        self.assertEqual(c['repetition']['mentioned']['matched'],[])
        self.assertEqual(c['repetition']['tagged']['matched'],[])
        self.assertEqual(c['repetition']['inferred']['matched'],['Ağaç'])
        self.assertEqual(c['provenance']['experience'],'eşzamanlılık')
        invalid=self.data('Bir yol vardı.')
        with self.assertRaisesRegex(ProviderError,'invalid_letter'): self.runone(invalid)
        from beta_reading import literal_mentions
        self.assertEqual(literal_mentions('Kuşak, evet, ışıklı, ayakkabı ve suç.'),{})
        self.assertEqual(set(literal_mentions('Görünen kuşların ve ağacın yanında ışığı gördüm.')),{'Kuş','Ağaç','Işık'})

    def test_legacy_receipts_replay_and_no_consent(self):
        data=self.data('Kapıya yaklaştım.',remember=True)
        a=self.runone(data)
        with self.db.transaction() as sql:
            sql('UPDATE beta_history_v1 SET symbols=? WHERE owner=? AND id=?',(json.dumps(['Kuş']),1,data['id']))
            sql('UPDATE beta_letters_v1 SET body=? WHERE owner=? AND id=?',(json.dumps({'letter':'Önceki mektup'}),1,data['id']))
        self.assertEqual(self.db.receipts(2),[])
        with self.assertRaisesRegex(ProviderError,'unknown_request'): self.db.receipt(2,data['id'])
        self.assertEqual(self.db.receipts(1),[{'id':data['id'],'ordinal':1}])
        self.assertEqual(self.db.receipt(1,data['id']),{'letter':'Önceki mektup'})
        self.assertEqual(self.db.submit(1,data,self.provider,0,paused=True),{'letter':'Önceki mektup'})
        self.assertEqual(len(self.ins),1)
        with self.assertRaisesRegex(ProviderError,'review_pause'):
            self.db.submit(1,self.data(),self.provider,3_000_000_000,paused=True)
        self.assertEqual(len(self.ins),1)
        b=self.data('Kapıdan geçtim.');b['symbols']=[]
        self.runone(b)
        past=self.ins[-1]['history'][0]
        self.assertEqual(past['experience'],'belirtilmedi')
        self.assertEqual(past['symbol_sources'],dict(mentioned=['Kapı'],tagged=['Kuş'],inferred=[]))
        self.runone(self.data(history=False))
        self.assertEqual(self.ins[-1]['history'],[])
        self.assertNotIn('calculated',self.ins[-1])
        self.assertEqual(len(self.db.receipts(1)),3)

    def test_closed_cap_before_transport_and_zero_spend_replay(self):
        budget=3_000_000_000
        data=self.data()
        self.runone(data)
        # Build an exhausted ledger in this isolated fixture and count dispatches.
        with self.db.transaction() as sql:
            sql('UPDATE beta_meter_v1 SET charge=?',(budget,))
        snapshot=self.db.budget_summary(budget)
        self.assertEqual(snapshot['remaining'],0)
        with self.assertRaisesRegex(ProviderError,'budget_exhausted'):
            self.runone(self.data(),owner=2,cap=budget)
        self.assertEqual(snapshot,self.db.budget_summary(budget))
        self.assertEqual(len(self.ins),1)
        self.assertEqual(self.runone(data),self.db.receipt(1,data['id']))
        self.assertEqual(len(self.ins),1)

    def test_rejection_rule_preserves_public_error_and_omits_input(self):
        ctx={'ordinal':1,'history':[],'current':'Tebeşir elimdeymiş.','symbol_sources':{'mentioned':[]}}
        base=dict(letter='Sözün burada.',observation=None,seal=None,sources=[],inferences=[])
        for raw,reason in [('{}','response_fields'),('not json','invalid_json'),
                           (json.dumps(base|{'sources':['K1']}),'history_reference'),
                           (json.dumps(base|{'letter':'Bir kuş gördün.'}),'unsupported_sighting'),
                           (json.dumps(base|{'inferences':[{'symbol':'Eşik','quote':'sahne dışı'}]}),'inference_fields'),
                           (json.dumps(base|{'inferences':[{'symbol':'Eşik','passage':'D9'}]}),'inference_reference')]:
            with self.assertRaises(ProviderError) as err: validate(raw,ctx)
            self.assertEqual((err.exception.code,err.exception.rule),('invalid_letter',reason))
        self.assertEqual(validate(json.dumps(base),ctx),base)
        from contextlib import redirect_stdout
        from io import StringIO
        self.guesses=[{'symbol':'Eşik','passage':'D9'}]
        narrative=self.data('Bunu loglara yazma.')
        captured=StringIO()
        with redirect_stdout(captured):
            for _ in range(2):
                with self.assertRaisesRegex(ProviderError,'invalid_letter'): self.runone(narrative)
        self.assertEqual(captured.getvalue().splitlines(),['KAIROS_LETTER_CHECK inference_reference'])
        self.assertEqual(self.db.receipts(1),[])
        self.assertEqual(len(self.ins),1)

    def test_structured_parts_use_the_exact_characters_and_only_one_message(self):
        base=dict(letter='Tebeşir.\nİkinci satır.',observation=None,seal=None,sources=[],inferences=[])
        raw=json.dumps(base)
        chunks=lambda s: {'type':'output_text','text':s}
        def message(parts):
            return dict(type='message',role='assistant',status='completed',content=parts)
        ctx=dict(ordinal=2,history=[],current='Tebeşir.')
        for cut in range(1,len(raw)):
            response={'status':'completed','output':[{'type':'reasoning'},message([chunks(raw[:cut]),chunks(raw[cut:])])]}
            self.assertEqual(validate(self.provider.output_text(response,structured=True),ctx),base)
        for output,code in [([message([chunks(raw)]),message([chunks(raw)])],'provider_invalid_response'),
                            ([message([chunks(raw),{'type':'refusal'}])],'provider_refused'),
                            ([message([chunks(raw)]),{'type':'function_call'}],'provider_invalid_response')]:
            with self.assertRaisesRegex(ProviderError,code):
                self.provider.output_text({'status':'completed','output':output},structured=True)
        for bad,rule in [(raw+raw,'invalid_json'),('~~~json\n'+raw+'\n~~~','invalid_json'),
                         ('{"letter":"ikinci",'+raw[1:],'duplicate_field')]:
            with self.assertRaises(ProviderError) as err: validate(bad,ctx)
            self.assertEqual(err.exception.rule,rule)

    def test_schema_limits_and_verbatim_span_for_repeated_passage(self):
        text='  Tebeşir elimdeymiş.\nTebeşir elimdeymiş.  '
        ctx=dict(ordinal=2,history=[],current=text)
        p=payload_for(MODEL,ctx)
        fields=p['text']['format']['schema']['properties']
        self.assertEqual(fields['sources']['maxItems'],0)
        prompts=json.loads(p['input'])['current']
        self.assertEqual(''.join(row['text'] for row in prompts),text)
        marked=[row['id'] for row in prompts if row['id']]
        self.assertEqual(fields['inferences']['items']['properties']['passage']['enum'],marked)
        self.assertNotIn('Tebeşir',json.dumps(fields,ensure_ascii=False))
        v=dict(inferences=[dict(symbol='İz',passage=marked[-1])],letter='Sınıftaki ayrıntıyı anlatıyorsun.',
               observation=None,sources=[],seal=None)
        a,b=validate(json.dumps(v),ctx)['inferences'][0]['basis']
        self.assertEqual((a,text[a:b]),(text.rindex('Tebeşir'),'Tebeşir elimdeymiş.'))
        for fake in ('K1',None,{},'D100'):
            v['inferences'][0]['passage']=fake
            with self.assertRaises(ProviderError) as err: validate(json.dumps(v),ctx)
            self.assertEqual(err.exception.rule,'inference_reference')
        ctx['history']=[{'id':'K1'},{'id':'K2'}]
        allowed=payload_for(MODEL,ctx)['text']['format']['schema']['properties']['sources']
        self.assertEqual(allowed['items']['enum'],['K1','K2'])
        self.assertEqual(allowed['maxItems'],2)

    def test_narrative_coverage_and_unusable_evidence(self):
        for text in ('I','🙂','x'*3000,('Çığ. '*700)[:3000],('İçimde bir şey var '*300)[:3000],
                     '\nBir kapı açıldı.\r\nAğaç.   ','uzun '+'a'*275+' son.'):
            parts=narrative_passages(text)
            self.assertEqual(''.join(chunk for _,chunk in parts),text)
            self.assertLessEqual(len(parts),49)
            self.assertTrue(all(2<=len(chunk.strip())<=120 for ref,chunk in parts if ref))
            if text in ('I','🙂','x'*3000):
                schema=payload_for(MODEL,dict(current=text,history=[],ordinal=1))['text']['format']['schema']
                self.assertEqual(schema['properties']['inferences']['maxItems'],0)

    def test_replay_fixture_preserves_provenance_without_extra_dispatch(self):
        story='Rüyamda kapısı aralık bir sınıfta ders anlatıyormuşum. Tebeşir elimdeymiş. Tahtaya ne yazacağımı hatırlamıyorum.'
        data=self.data(story,history=False)
        data.update(experience='rüya',symbols=['Kuş'])
        self.guesses=[{'symbol':'İz','passage':'D2'}]
        result=self.runone(data)
        p=result['console']['provenance']
        self.assertEqual((list(p['mentioned']),p['tagged']),(['Kapı'],['Kuş']))
        a,b=p['inferred'][0]['basis']
        self.assertEqual(story[a:b],'Tebeşir elimdeymiş.')
        self.assertEqual(self.runone(data),self.db.receipt(1,data['id']))
        self.assertEqual(len(self.ins),1)
        with self.db.transaction() as sql:
            self.assertEqual(sql('SELECT COUNT(*) FROM beta_history_v1').fetchone()[0],0)

    def test_readiness(self):
        with patch.dict('os.environ',{},clear=True): self.assertEqual(readiness(),'missing_key')
        with patch.dict('os.environ',{'OPENAI_API_KEY':'fixture'},clear=True): self.assertEqual(readiness(),'budget_closed')
