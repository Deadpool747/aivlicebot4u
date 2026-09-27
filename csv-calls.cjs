const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');

function parseLeads(text){
 if(typeof text!=='string'||Buffer.byteLength(text)>256000)throw Error('Use a CSV smaller than 256 KB.');
 const rows=[];let row=[],cell='',quoted=false,closed=false;
 text=text.replace(/^\uFEFF/,'');
 for(let i=0;i<=text.length;i++){
  const c=text[i];
  if(quoted){if(c===undefined)throw Error('Unclosed quote in CSV.');if(c==='"'){if(text[i+1]==='"'){cell+='"';i++}else{quoted=false;closed=true}}else cell+=c;continue}
  if(c==='"'&&!cell&&!closed){quoted=true;continue}
  if(c===','||c==='\n'||c===undefined){row.push(cell.trim());cell='';closed=false;if(c!==','){if(row.some(Boolean))rows.push(row);row=[]}continue}
  if(c==='\r')continue;
  if(closed&&c.trim())throw Error('Unexpected text after a quoted CSV value.');
  cell+=c;
 }
 if(rows.length<2)throw Error('Include a header and at least one lead.');
 if(rows.length>201)throw Error('Import up to 200 leads at a time.');
 const headers=rows.shift().map(h=>h.toLowerCase().replace(/[ _-]/g,''));
 const phones=headers.map((h,i)=>['phone','phonenumber','number','mobile','leadphone'].includes(h)?i:-1).filter(i=>i>=0);
 const names=headers.map((h,i)=>['name','fullname','customername','leadname'].includes(h)?i:-1).filter(i=>i>=0);
 if(phones.length!==1||names.length>1)throw Error('Use one phone column and optionally one name column.');
 const seen=new Set();
 return rows.map((r,i)=>{
  const number=(r[phones[0]]||'').replace(/[ ()-]/g,''),name=names.length?(r[names[0]]||''):'';
  let error=r.length!==headers.length?'Column count does not match the header.':'';
  if(!/^\+[1-9]\d{6,14}$/.test(number))error='Include + and country code in the phone number.';
  else if(name&&!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(name))error='Name contains unsupported characters or is too long.';
  else if(seen.has(number))error='Duplicate phone number.';
  seen.add(number);return {row:i+2,name,number,error,state:error?'Skipped':'Ready'};
 });
}

function createCampaigns({directory,call,history,now=Date.now,loadSheet=null,sheetPollMs=30000}){
 fs.mkdirSync(directory,{recursive:true});const jobs=new Map(),busy=new Set(),syncing=new Set();
 const file=owner=>path.join(directory,crypto.createHash('sha256').update(owner).digest('hex')+'.json');
 const save=(owner,j)=>{fs.writeFileSync(file(owner)+'.tmp',JSON.stringify(j),{mode:0o600});fs.renameSync(file(owner)+'.tmp',file(owner))};
 function get(owner){if(!jobs.has(owner)){let j=null;try{j=JSON.parse(fs.readFileSync(file(owner),'utf8'));if(j.running&&j.source?.type!=='google-sheet'){j.running=false;j.message='Server restarted. Review the current call before resuming.';save(owner,j)}}catch{}jobs.set(owner,j)}return jobs.get(owner)}
 const locked=owner=>{const j=get(owner);return !!(j&&(j.running||j.current!==null))};
 function importCsv(owner,text){if(locked(owner))throw Error('Pause and finish the current call before replacing the list.');const leads=parseLeads(text);const j={id:crypto.randomUUID(),owner,leads,running:false,current:null,source:{type:'csv'},message:'Preview ready. Skipped rows will not be called.'};jobs.set(owner,j);save(owner,j);return j}
 function mergeSheet(owner,csv,source){
  const incoming=parseLeads(csv),current=get(owner),same=current?.source?.type==='google-sheet'&&current.source.url===source.url;
  if(current&&locked(owner)&&!same)throw Error('Pause and finish the current call before connecting another Google Sheet.');
  if(!same){const ready=incoming.filter(lead=>!lead.error).length,j={id:crypto.randomUUID(),owner,leads:incoming,running:ready>0,current:null,source:{type:'google-sheet',url:source.url,exportUrl:source.exportUrl,lastSyncAt:new Date(now()).toISOString(),autoStart:true},message:ready?`${ready} Google Sheet leads imported. Calling started automatically.`:'No valid leads were found in this Google Sheet.'};jobs.set(owner,j);save(owner,j);return j}
  const seen=new Set(current.leads.map(lead=>lead.number)),space=Math.max(0,200-current.leads.length),additions=incoming.filter(lead=>!lead.error&&!seen.has(lead.number)).slice(0,space);
  for(const lead of additions){lead.row=current.leads.length+1;current.leads.push(lead)}
  current.source={...current.source,url:source.url,exportUrl:source.exportUrl,lastSyncAt:new Date(now()).toISOString(),lastError:''};
  if(additions.length){current.running=current.source.autoStart!==false;current.message=current.running?`${additions.length} new Google Sheet lead${additions.length===1?'':'s'} added. Calling automatically.`:`${additions.length} new Google Sheet lead${additions.length===1?'':'s'} added. The list remains paused.`}
  else if(current.current===null&&!current.running)current.message='Google Sheet is connected. Waiting for new leads.';
  save(owner,current);return current;
 }
 async function saveSheet(owner,url){if(!loadSheet)throw Error('Google Sheet calling is unavailable.');const source=await loadSheet(url);return mergeSheet(owner,source.csv,source)}
 async function refreshSheet(owner){const j=get(owner);if(!loadSheet||j?.source?.type!=='google-sheet'||syncing.has(owner))return j;const checked=Date.parse(j.source.lastSyncAt||0);if(now()-checked<sheetPollMs)return j;syncing.add(owner);try{const source=await loadSheet(j.source.url);return mergeSheet(owner,source.csv,source)}catch(e){j.source.lastSyncAt=new Date(now()).toISOString();j.source.lastError=e.message;j.message=`Google Sheet sync failed: ${e.message}`;save(owner,j);return j}finally{syncing.delete(owner)}}
 function action(owner,action){const j=get(owner);if(!j)throw Error('Import a CSV or connect a Google Sheet first.');if(action==='pause'){j.running=false;if(j.source?.type==='google-sheet')j.source.autoStart=false;j.message='Paused. A submitted call may still finish.'}else if(action==='start'){if(!j.leads.some(l=>l.state==='Ready')&&j.current===null)throw Error('No remaining valid leads.');if(j.current!==null&&j.leads[j.current].state==='Unconfirmed')throw Error('Confirm that the current call ended before continuing.');j.running=true;if(j.source?.type==='google-sheet')j.source.autoStart=true;j.message='Calling list started.'}else if(action==='confirm-ended'){if(j.current===null||j.leads[j.current].state!=='Unconfirmed')throw Error('No unconfirmed call to resolve.');j.leads[j.current].state='Unconfirmed — reviewed';j.current=null;j.running=false;if(j.source?.type==='google-sheet')j.source.autoStart=false;j.message='Call marked reviewed. Click Start calling to continue.'}else throw Error('Unknown action.');save(owner,j);return j}
 async function tick(owner){if(busy.has(owner))return;busy.add(owner);try{const j=get(owner);if(!j)return;
  if(j.current!==null){const lead=j.leads[j.current];const done=history.list(owner).find(r=>r.id===lead.historyId&&r.callId&&r.duration!==null);
   if(done){lead.state=done.result;lead.message='Call completed.';j.current=null;save(owner,j)}
   else{if(now()-lead.submittedAt>390000){lead.state='Unconfirmed';j.running=false;j.message='Carrier completion was not confirmed. Check Piopiy, then confirm the call ended before continuing.';save(owner,j)}return}
  }
  if(!j.running)return;const i=j.leads.findIndex(l=>l.state==='Ready');if(i<0){j.running=false;j.message='Calling list complete.';save(owner,j);return}
  const lead=j.leads[i];j.current=i;lead.state='Submitting';lead.submittedAt=now();save(owner,j);
  let result;try{result=await call(owner,{number:lead.number,name:lead.name})}catch{result={status:502,body:{error:'Call submission could not be confirmed.',uncertain:true}}}
  lead.historyId=result.body.historyId;lead.message=result.body.message||result.body.error;
  if(result.status===202){lead.state='Queued';j.message='Waiting for the current call to finish.'}
  else if(result.body.uncertain){lead.state='Unconfirmed';j.running=false;if(j.source?.type==='google-sheet')j.source.autoStart=false;j.message=lead.message+' Check the carrier before continuing.'}
  else{lead.state='Ready';j.current=null;j.running=false;if(j.source?.type==='google-sheet')j.source.autoStart=false;j.message=lead.message}
  save(owner,j);
 }finally{busy.delete(owner)}}
 for(const name of fs.readdirSync(directory)){if(!name.endsWith('.json'))continue;try{const j=JSON.parse(fs.readFileSync(path.join(directory,name),'utf8'));if(j.owner&&j.source?.type==='google-sheet')jobs.set(j.owner,j)}catch{}}
 const timer=setInterval(()=>{for(const owner of jobs.keys())refreshSheet(owner).then(()=>tick(owner)).catch(()=>{const j=get(owner);if(j){j.running=false;if(j.source?.type==='google-sheet')j.source.autoStart=false;j.message='Queue paused after a storage error.';save(owner,j)}})},2000);timer.unref();
 return {get,importCsv,saveSheet,refreshSheet,action,locked,tick,close:()=>clearInterval(timer)};
}
module.exports={parseLeads,createCampaigns};
