(()=>{
 const el=id=>document.getElementById(id);let busy=false,campaign=null,dirty=false;
 function render(j){campaign=j;el('csvRows').replaceChildren();
  if(j)for(const lead of j.leads){const tr=document.createElement('tr');for(const text of [lead.name||'—',lead.number,lead.error||lead.state]){const td=document.createElement('td');td.textContent=text;td.style.padding='8px';tr.append(td)}el('csvRows').append(tr)}
  el('csvSummary').textContent=j?`${j.leads.length} rows · ${j.leads.filter(l=>l.state==='Ready').length} ready · ${j.leads.filter(l=>l.error).length} skipped`:'';
  el('csvStatus').textContent=j?.message||'Import your leads to begin.';
  const locked=!!j&&(j.running||j.current!==null);
  el('csvImport').disabled=busy||locked;el('csvFile').disabled=busy||locked;el('csvText').disabled=busy||locked;
  el('csvStart').disabled=busy||dirty||!j||j.running||(!j.leads.some(l=>l.state==='Ready')&&j.current===null)||j.leads[j.current]?.state==='Unconfirmed';
  el('csvPause').disabled=busy||!j?.running;
  el('csvResolve').hidden=j?.current==null||j.leads[j.current]?.state!=='Unconfirmed';el('csvResolve').disabled=busy;
 }
 async function request(action,extra={}){if(busy)return;busy=true;render(campaign);try{const r=await fetch('api/phone/campaign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...extra})});const d=await r.json();if(!r.ok)throw Error(d.error||'Could not update the calling list.');campaign=d.campaign;if(action==='import')dirty=false;busy=false;render(campaign)}catch(e){busy=false;render(campaign);el('csvStatus').textContent=e.message}}
 el('csvFile').onchange=async()=>{const file=el('csvFile').files[0];if(!file)return;if(file.size>256000){el('csvStatus').textContent='Choose a CSV smaller than 256 KB.';return}try{const csv=await file.text();el('csvText').value=csv;await request('import',{csv})}catch{el('csvStatus').textContent='Could not read this file.'}};
 el('csvText').oninput=()=>{dirty=true;render(campaign);el('csvStatus').textContent='CSV changed. Click Preview leads before calling.'};
 el('csvImport').onclick=()=>request('import',{csv:el('csvText').value});
 el('csvStart').onclick=()=>{if(savingScript||!scriptLoaded||(mode==='outbound'&&el('script').value!==savedScript)){el('csvStatus').textContent='Save your script changes before starting calls.';return}request('start')};
 el('csvPause').onclick=()=>request('pause');el('csvResolve').onclick=()=>request('confirm-ended');
 async function refresh(){if(busy)return;try{const r=await fetch('api/phone/campaign');if(!r.ok)throw Error();const d=await r.json();if(!busy)render(d.campaign)}catch{el('csvStatus').textContent='Could not refresh the calling list. Refresh this page to check its status.'}}
 refresh();setInterval(refresh,5000);
})();
