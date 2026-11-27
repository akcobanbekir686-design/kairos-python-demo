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
const last='kairos.brain.last.v1';let ready=false,submitted=!!sessionStorage.getItem(last),busy=false;
let retryId=null,canReset=false;
function controls(){
 el('go').disabled=!ready||submitted||busy;
 el('recover').hidden=!sessionStorage.getItem(last);el('recover').disabled=busy;
 el('new').hidden=!canReset;el('new').disabled=busy;
 el('archive').disabled=busy;el('pass').disabled=busy;
}
el('recover').hidden=!submitted;el('new').hidden=!canReset;
function show(r){
 const c=r.console||{};
 const lines=c.origin?[c.origin]:[];
 const p=c.provenance;
 if(p){
   lines.push('Deneyim türü · '+p.experience);
   for(const [key,title,names] of [
     ['mentioned','Metinde geçen',Object.keys(p.mentioned)],
     ['tagged','İşaretlenen etiket',p.tagged],
     ['inferred','Modelin olası çağrışımı',p.inferred.map(item=>item.symbol)]
   ]){
     const match=c.repetition?.[key];
     const repeated=match&&match.ratio[1]?' · tekrar '+match.ratio.join(' / '):'';
     lines.push(title+' · '+(names.join(', ')||'yok')+repeated);
   }
   if(c.history_status==='kapalı')lines.push('Geçmiş paylaşımı kapalı.');
   for(const s of c.sources||[])lines.push(s.id+' · '+s.experience+' · kaynak türüne göre benzerlik '+s.cosine);
 }else{
   if(r.console)lines.push(c.ratio?'Tekrarlanan etiket / seçilen etiket: '+c.ratio.join(' / '):'Geçmiş paylaşımı kapalı.');
   for(const s of c.sources||[])lines.push(s.id+' · '+s.symbols.join(', ')+' · etiket benzerliği '+s.cosine);
 }
 el('console').textContent=lines.join('\n');
 for(const id of ['letter','observation','seal'])el(id).textContent=r[id]||'';
 el('obsbox').hidden=!r.observation;el('sealbox').hidden=!r.seal;el('result').hidden=false;el('msg').textContent='';
}
async function jsonCall(path, options, timeout){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),timeout);
 try{
   const response=await fetch(path,{...options,cache:'no-store',signal:controller.signal});
   try{return {response,data:await response.json()};}
   catch{throw new Error('Sunucudan okunabilir yanıt gelmedi (HTTP '+response.status+').');}
 }catch(error){
   if(controller.signal.aborted)throw new Error('Sunucu zamanında yanıt vermedi.');
   if(error instanceof TypeError)throw new Error('Tarayıcı test servisine ulaşamadı.');
   throw error;
 }finally{clearTimeout(timer);}
}

async function readSaved(headers,id,afterDisconnect){
 const delays=afterDisconnect?[0,2000,5000,10000]:[0];
 let lastError;
 for(const delay of delays){
   if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
   try{
     const reply=await jsonCall('/receipt',{method:'POST',headers,body:JSON.stringify({id})},20000);
     if(delay===delays[delays.length-1]||!['unknown_request','already_submitted'].includes(reply.data.error))return reply;
   }catch(error){lastError=error;if(delay===delays[delays.length-1])throw error;}
 }
 throw lastError;
}

async function send(recover,savedId=null){
 if(busy||(!recover&&(!ready||submitted)))return;
 const pass=el('pass').value.trim();
 if(!pass){el('msg').textContent='Test giriş kodunu gir.';return;}
 let headers;
 try{headers=new Headers({'Content-Type':'application/json',Authorization:'Bearer '+pass});}
 catch{el('msg').textContent='Test giriş kodunda geçersiz bir karakter var. Kodu yeniden yapıştır.';return;}
 const id=recover?(savedId||sessionStorage.getItem(last)):(retryId||crypto.randomUUID());
 if(!id)return;
 const draft=recover?null:{id,text:el('narrative').value,symbols:Array.from(el('symbols').querySelectorAll('input:checked'),n=>n.value),
   experience:el('experience').value,share:el('share').checked,history:el('history').checked,remember:el('remember').checked};
 if(draft&&(!draft.share||!draft.text.trim())){el('msg').textContent='Anlatını yaz ve paylaşım iznini kontrol et.';return;}
 busy=true;controls();
 let checking=recover;
 try{
   let reply;
   if(draft){
     el('msg').textContent='Test servisinin hazır olması bekleniyor…';
     const warm=await jsonCall('/status',{},90000);
     if(!warm.response.ok||warm.data.state!=='configured'){
       el('msg').textContent=errors[warm.data.state]||'Test servisi hazır değil.';return;
     }
     if(!el('share').checked||draft.text!==el('narrative').value||
         draft.history!==el('history').checked||draft.remember!==el('remember').checked||
         draft.experience!==el('experience').value||
         JSON.stringify(draft.symbols)!==JSON.stringify(Array.from(el('symbols').querySelectorAll('input:checked'),n=>n.value))){
       el('msg').textContent='Taslak veya izinlerin değişti. Hazır olduğunda tekrar onaylayabilirsin.';return;
     }
     sessionStorage.setItem(last,id);submitted=true;canReset=false;controls();
     el('msg').textContent='Mektup hazırlanıyor…';
     try{
       reply=await jsonCall('/reading',{method:'POST',headers,body:JSON.stringify(draft)},75000);
     }catch(error){
       checking=true;
       el('msg').textContent=error.message+' Aynı isteğin kaydı kontrol ediliyor…';
       reply=await readSaved(headers,id,true);
     }
   }else{
     el('msg').textContent='Kayıtlı sonuç kontrol ediliyor…';
     reply=await readSaved(headers,id,false);
   }
   const {response,data}=reply;
   if(!response.ok){
     if(checking&&!savedId&&data.error==='unknown_request'){
       retryId=id;submitted=false;canReset=false;
       el('msg').textContent='Bu kodla istek kaydı bulunamadı. Taslağın korunuyor. İstersen aynı istekle tekrar deneyebilirsin.';
     }else{
       if(!savedId&&data.error&&!['already_submitted','storage_unavailable','beta_only'].includes(data.error))canReset=true;
       el('msg').textContent=errors[data.error]||'İstek tamamlanamadı (HTTP '+response.status+').';
     }
     return;
   }
   try{show(data);}catch{el('msg').textContent='Mektup bulundu ancak görüntülenemedi. Kayıtlı mektupları açabilirsin.';return;}
   if(!savedId){sessionStorage.removeItem(last);retryId=null;submitted=false;canReset=false;el('share').checked=false;}
 }catch(error){
   el('msg').textContent=error.message+(checking?' Kayıtlı sonucu tekrar kontrol edebilirsin.':'');
 }finally{busy=false;controls();}
}
el('archive').onclick=async()=>{
 const pass=el('pass').value.trim(),list=el('letters');
 list.replaceChildren();
 if(!pass){el('msg').textContent='Test giriş kodunu gir.';return;}
 try{
   const response=await fetch('/receipts',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+pass},body:'{}',cache:'no-store'});
   const data=await response.json();if(el('pass').value.trim()!==pass)return;
   if(!response.ok){el('msg').textContent=errors[data.error]||'Mektuplar yüklenemedi.';return;}
   el('msg').textContent=data.items.length?'Bu test hesabında tamamlanan mektup sayısı: '+data.items.length:'Bu hesapta kayıtlı mektup yok.';
   for(const item of data.items){
     const button=document.createElement('button');button.className='alt';button.textContent='Mektup '+item.ordinal;
     button.onclick=()=>send(true,item.id);list.append(button);
   }
 }catch{el('msg').textContent='Mektuplar yüklenemedi.';}
};
el('pass').oninput=()=>{el('letters').replaceChildren();el('result').hidden=true;el('msg').textContent='';};
el('go').onclick=()=>send(false);el('recover').onclick=()=>send(true);
el('new').onclick=()=>{if(busy||!canReset)return;retryId=null;submitted=false;canReset=false;sessionStorage.removeItem(last);el('narrative').value='';el('experience').selectedIndex=0;document.querySelectorAll('input[type=checkbox]').forEach(n=>n.checked=false);
 el('recover').hidden=true;el('new').hidden=true;el('result').hidden=true;el('msg').textContent='';el('go').disabled=!ready;};
fetch('/status').then(r=>r.json()).then(s=>{ready=s.state==='configured';el('status').textContent=ready?'Test servisi hazır · '+s.model:errors[s.state];controls();}).catch(()=>{el('status').textContent='Test servisine erişilemiyor.'});
