const el=id=>document.getElementById(id);
const errors={
 missing_key:'Test servisinin OpenAI anahtarı henüz eklenmedi.',
 budget_closed:'Test bütçesi henüz onaylanmadı. Ücretli çağrı kapalı.',
 model_price_unverified:'Seçili modelin tarifesi doğrulanmadı.',
 provider_key_rejected:'OpenAI proje anahtarını kabul etmedi.',
 provider_model_unavailable:'Projenin bu modele veya Responses API’ye erişimini kontrol et.',
 budget_exhausted:'Kalan bütçe yeni istek için yeterli değil.',
 tester_quota:'Test hesabının çağrı sınırına ulaşıldı.',busy:'Başka bir mektup hazırlanıyor.',
 cluster_complete:'Bu test hesabının 21 mektubu tamamlandı.',
 consent_required:'Bu mektup için anlatını paylaşmaya izin vermelisin.',
 beta_only:'Test giriş kodunu kontrol et.',invalid_request:'Anlatını ve en az bir sembolü kontrol et.',
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
 el('console').textContent=[c.origin,c.ratio?'Tekrarlanan sembol / seçilen sembol: '+c.ratio.join(' / '):'Geçmiş paylaşımı kapalı.',
 ...c.sources.map(s=>[s.id,s.symbols.join(', '),'seçili sembollerin benzerliği '+s.cosine].join(' · '))].join('\n');
 for(const id of ['letter','observation','seal'])el(id).textContent=r[id]||'';
 el('obsbox').hidden=!r.observation;el('sealbox').hidden=!r.seal;el('result').hidden=false;el('msg').textContent='';
}
async function send(recover){
 const pass=el('pass').value.trim();
 if(!pass){el('msg').textContent='Test giriş kodunu gir.';return}
 const symbols=Array.from(el('symbols').querySelectorAll('input:checked'),n=>n.value);
 if(!recover&&(!el('share').checked||!el('narrative').value.trim()||!symbols.length)){
   el('msg').textContent='Anlatını yaz, en az bir sembol seç ve paylaşım iznini kontrol et.';return;
 }
 const id=recover?sessionStorage.getItem(last):crypto.randomUUID();
 if(!id)return;
 if(!recover){sessionStorage.setItem(last,id);submitted=true;el('recover').hidden=false;}
 el('go').disabled=true;el('recover').disabled=true;el('msg').textContent=recover?'Son istek kontrol ediliyor…':'Konsol çalışıyor, mektup bekleniyor…';
 try{
   const r=await fetch(recover?'/receipt':'/reading',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+pass},
     body:JSON.stringify(recover?{id}:{id,text:el('narrative').value,symbols,share:el('share').checked,history:el('history').checked,remember:el('remember').checked})});
   const data=await r.json();if(!r.ok){el('msg').textContent=errors[data.error]||'İstek tamamlanamadı. Kod: '+data.error;return;}
   submitted=true;show(data);
 }catch{el('msg').textContent='Bağlantı kesildi. Son isteği kontrol et.';}
 finally{el('recover').disabled=false;el('go').disabled=!ready||submitted;el('new').hidden=!submitted;}
}
el('go').onclick=()=>send(false);el('recover').onclick=()=>send(true);
el('new').onclick=()=>{submitted=false;sessionStorage.removeItem(last);el('narrative').value='';document.querySelectorAll('input[type=checkbox]').forEach(n=>n.checked=false);
 el('recover').hidden=true;el('new').hidden=true;el('result').hidden=true;el('msg').textContent='';el('go').disabled=!ready;};
fetch('/status').then(r=>r.json()).then(s=>{ready=s.state==='configured';el('status').textContent=ready?'Test servisi hazır · '+s.model:errors[s.state];el('go').disabled=!ready||submitted;}).catch(()=>{el('status').textContent='Test servisine erişilemiyor.'});
