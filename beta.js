const el=id=>document.getElementById(id);
const errors={
 review_pause:'Kayıtlı mektuplar açık. Yeni yorum için beta onayı bekleniyor.',
 missing_key:'Test servisinin OpenAI anahtarı henüz eklenmedi.',
 budget_closed:'Test bütçesi henüz onaylanmadı. Ücretli çağrı kapalı.',
 model_price_unverified:'Seçili modelin tarifesi doğrulanmadı.',
 provider_key_rejected:'OpenAI proje anahtarını kabul etmedi.',
 provider_model_unavailable:'Projenin bu modele veya Responses API’ye erişimini kontrol et.',
 budget_exhausted:'Kalan bütçe yeni istek için yeterli değil.',
 tester_quota:'Test hesabının çağrı sınırına ulaşıldı.',busy:'Başka bir mektup hazırlanıyor.',
 cluster_complete:'Bu test hesabının 21 mektubu tamamlandı.',
 consent_required:'Bu mektup için anlatını paylaşmaya izin vermelisin.',
 beta_only:'Test giriş kodunu kontrol et.',invalid_request:'Anlatını ve seçimlerini kontrol et.',
 already_submitted:'İstek işleme alındı. Son isteği kontrol edebilirsin.',
 provider_incomplete:'Yanıt tamamlanamadı. Aynı istek yeniden ücretlendirilmez.',
 unknown_request:'Bu kodla kaydedilmiş istek bulunamadı.',
 invalid_letter:'Yanıt mektup biçimine uymadı.',storage_unavailable:'Test hafızasına şu anda erişilemiyor.',
 provider_rate_limited:'OpenAI proje kotası dolu olabilir.',provider_timeout:'Yanıt süresi doldu.',
 usage_unconfirmed:'Token kullanımı doğrulanamadı.',provider_rejected:'OpenAI bu model isteğini kabul etmedi.'
};
for(const s of ['Ay','Su','Kapı','Yol','Ağaç','Işık','Ev','Kuş']){
 const label=document.createElement('label'),field=document.createElement('input');field.type='checkbox';field.value=s;label.append(field,document.createTextNode(s));el('symbols').append(label);
}
const last='kairos.brain.last.v1';let ready=false,submitted=!!sessionStorage.getItem(last);
el('recover').hidden=!submitted;el('new').hidden=!submitted;
function show(r){
 const c=r.console;
 const lines=[c.origin];
 const p=c.provenance;
 if(p){
   lines.push('Deneyim türü · '+p.experience);
   for(const [key,title,names] of [
     ['mentioned','Metinde geçen',Object.keys(p.mentioned)],
     ['tagged','İşaretlenen etiket',p.tagged],
     ['inferred','Modelin olası çağrışımı',p.inferred.map(item=>item.symbol)]
   ]){
     const match=c.repetition[key];
     const repeated=match&&match.ratio[1]?' · tekrar '+match.ratio.join(' / '):'';
     lines.push(title+' · '+(names.join(', ')||'yok')+repeated);
   }
   if(c.history_status==='kapalı')lines.push('Geçmiş paylaşımı kapalı.');
   for(const s of c.sources)lines.push(s.id+' · '+s.experience+' · kaynak türüne göre benzerlik '+s.cosine);
 }else{
   lines.push(c.ratio?'Tekrarlanan etiket / seçilen etiket: '+c.ratio.join(' / '):'Geçmiş paylaşımı kapalı.');
   for(const s of c.sources)lines.push(s.id+' · '+s.symbols.join(', ')+' · etiket benzerliği '+s.cosine);
 }
 el('console').textContent=lines.join('\n');
 for(const id of ['letter','observation','seal'])el(id).textContent=r[id]||'';
 el('obsbox').hidden=!r.observation;el('sealbox').hidden=!r.seal;el('result').hidden=false;el('msg').textContent='';
}
async function send(recover,savedId=null){
 const pass=el('pass').value.trim();
 if(!pass){el('msg').textContent='Test giriş kodunu gir.';return}
 const symbols=Array.from(el('symbols').querySelectorAll('input:checked'),n=>n.value);
 if(!recover&&(!el('share').checked||!el('narrative').value.trim())){
   el('msg').textContent='Anlatını yaz ve paylaşım iznini kontrol et.';return;
 }
 const id=recover?(savedId||sessionStorage.getItem(last)):crypto.randomUUID();
 if(!id)return;
 if(!recover){sessionStorage.setItem(last,id);submitted=true;el('recover').hidden=false;}
 el('go').disabled=true;el('recover').disabled=true;el('msg').textContent=recover?'Son istek kontrol ediliyor…':'Konsol çalışıyor, mektup bekleniyor…';
 try{
   const r=await fetch(recover?'/receipt':'/reading',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+pass},
     body:JSON.stringify(recover?{id}:{id,text:el('narrative').value,symbols,experience:el('experience').value,share:el('share').checked,history:el('history').checked,remember:el('remember').checked})});
   const data=await r.json();if(!r.ok){el('msg').textContent=errors[data.error]||'İstek tamamlanamadı. Kod: '+data.error;return;}
   if(el('pass').value.trim()!==pass)return;
   sessionStorage.setItem(last,id);el('recover').hidden=false;submitted=true;show(data);
 }catch{el('msg').textContent='Bağlantı kesildi. Son isteği kontrol et.';}
 finally{el('recover').disabled=false;el('go').disabled=!ready||submitted;el('new').hidden=!submitted;}
}
el('archive').onclick=async()=>{
 const pass=el('pass').value.trim(),list=el('letters');
 list.replaceChildren();
 if(!pass){el('msg').textContent='Test giriş kodunu gir.';return;}
 try{
   const response=await fetch('/receipts',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+pass},body:'{}',cache:'no-store'});
   const data=await response.json();if(el('pass').value.trim()!==pass)return;
   if(!response.ok){el('msg').textContent=errors[data.error]||'Mektuplar yüklenemedi.';return;}
   el('msg').textContent=data.items.length?'':'Bu hesapta kayıtlı mektup yok.';
   for(const item of data.items){
     const button=document.createElement('button');button.className='alt';button.textContent='Mektup '+item.ordinal;
     button.onclick=()=>send(true,item.id);list.append(button);
   }
 }catch{el('msg').textContent='Mektuplar yüklenemedi.';}
};
el('pass').oninput=()=>{el('letters').replaceChildren();el('result').hidden=true;el('msg').textContent='';};
el('go').onclick=()=>send(false);el('recover').onclick=()=>send(true);
el('new').onclick=()=>{submitted=false;sessionStorage.removeItem(last);el('narrative').value='';el('experience').selectedIndex=0;document.querySelectorAll('input[type=checkbox]').forEach(n=>n.checked=false);
 el('recover').hidden=true;el('new').hidden=true;el('result').hidden=true;el('msg').textContent='';el('go').disabled=!ready;};
fetch('/status').then(r=>r.json()).then(s=>{ready=s.state==='configured';el('status').textContent=ready?'Test servisi hazır · '+s.model:errors[s.state];el('go').disabled=!ready||submitted;}).catch(()=>{el('status').textContent='Test servisine erişilemiyor.'});
