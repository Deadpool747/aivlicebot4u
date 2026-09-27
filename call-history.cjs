const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
function createHistory(root){
 let lastAt=0;
 const dir=owner=>path.join(root,crypto.createHash('sha256').update(owner.toLowerCase()).digest('hex'));
 function add(owner,data){const id=crypto.randomUUID(),folder=dir(owner);fs.mkdirSync(folder,{recursive:true});fs.writeFileSync(path.join(folder,id+'.json'),JSON.stringify({id,at:new Date(lastAt=Math.max(Date.now(),lastAt+1)).toISOString(),...data}));return id}
 function list(owner){const folder=dir(owner);if(!fs.existsSync(folder))return [];const events=fs.readdirSync(folder).filter(f=>f.endsWith('.json')).flatMap(f=>{try{return [JSON.parse(fs.readFileSync(path.join(folder,f),'utf8'))]}catch{return []}}).sort((a,b)=>a.at.localeCompare(b.at));const rows=new Map();
 for(const e of events){if(e.kind==='request'){rows.set(e.id,{id:e.id,date:e.at,type:e.type||'outbound',source:e.source||'phone',phone:e.phone,name:e.name||'',duration:null,result:e.result||'Pending',remarks:e.remarks||'',followUpAttempt:e.followUpAttempt||undefined})}else if(e.kind==='session'){
 let row=rows.get(e.requestId);if(!row&&e.type==='outbound'&&e.source!=='browser')row=[...rows.values()].reverse().find(r=>r.type==='outbound'&&r.source!=='browser'&&r.phone.replace(/\D/g,'')===e.phone.replace(/\D/g,'')&&!r.callId&&Math.abs(Date.parse(e.at)-Date.parse(r.date))<300000);
 if(!row){row={id:e.id,date:e.startedAt||e.at,type:e.type,source:e.source||'phone',phone:e.phone,name:e.name||'',duration:null,result:'Pending',remarks:''};rows.set(row.id,row)}
 Object.assign(row,{callId:e.callId,duration:e.duration,result:e.result,remarks:e.remarks,recordingId:e.recordingId||null});if(e.name)row.name=e.name;
 }}
 const notes=path.join(folder,'notes'),active=new Map();
 for(const row of rows.values()){
  let note={};try{note=JSON.parse(fs.readFileSync(path.join(notes,row.id+'.json'),'utf8'));Object.assign(row,note)}catch{}
  if(row.result==='Pending'&&Date.now()-Date.parse(row.date)>360000)row.result='Unconfirmed';
  const key=String(row.phone||'').replace(/\D/g,'')||row.id,previous=active.get(key),followUp=/^follow\s*up$/i.test(String(row.remarks||'').trim()),missed=['Not answered','Unconfirmed'].includes(row.result),explicit=[1,2,3,4].includes(Number(row.followUpAttempt));
  if(row.result==='Answered'&&!followUp&&!Object.hasOwn(note,'followUpAttempt')){if(previous)delete previous.followUpAttempt;delete row.followUpAttempt;active.delete(key);continue}
  if(explicit||followUp||missed){const next=Math.min(4,Number(previous?.followUpAttempt||0)+1),attempt=explicit?Math.min(4,Math.max(Number(row.followUpAttempt),next)):next;if(previous&&previous!==row)delete previous.followUpAttempt;row.followUpAttempt=attempt;active.set(key,row)}
 }
 return [...rows.values()].sort((a,b)=>b.date.localeCompare(a.date));}
 function edit(owner,id,data){if(!/^[\da-f-]{36}$/.test(id)||!list(owner).some(r=>r.id===id))return false;const folder=path.join(dir(owner),'notes');fs.mkdirSync(folder,{recursive:true});const dest=path.join(folder,id+'.json');let prior={};try{prior=JSON.parse(fs.readFileSync(dest,'utf8'))}catch{}const value={...prior};if(Object.hasOwn(data,'remarks'))value.remarks=data.remarks;if(data.result)value.result=data.result;if(Object.hasOwn(data,'followUpAttempt'))value.followUpAttempt=data.followUpAttempt;fs.writeFileSync(dest+'.tmp',JSON.stringify(value));fs.renameSync(dest+'.tmp',dest);return true}
 function recording(owner,id){const row=list(owner).find(r=>r.id===id);if(!row||!/^[-a-f0-9]{36}$/.test(row.recordingId||''))return null;const file=path.join(dir(owner),'recordings',row.recordingId+'.wav');return fs.existsSync(file)?file:null}
 function publicList(owner){return list(owner).map(row=>({...row,result:row.result==='Answered'?'Answered':'Not answered'}))}
 return {add,list,publicList,edit,recording};
}
module.exports={createHistory};
