const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');

const STATUSES=['Scheduled','Calling','Completed','No Answer','Failed','Retry Scheduled','Cancelled','Rescheduled'];
const ACTIVE=new Set(['Scheduled','Retry Scheduled','Rescheduled']);
const EDITABLE=new Set(['Scheduled','Retry Scheduled','Rescheduled','No Answer','Failed']);

function validZone(zone){try{new Intl.DateTimeFormat('en-US',{timeZone:zone}).format();return true}catch{return false}}
function zoneParts(date,zone){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date);return Object.fromEntries(parts.filter(p=>p.type!=='literal').map(p=>[p.type,Number(p.value)]))}
function zonedDateTime(dateValue,timeValue,zone){
 if(typeof dateValue!=='string'||!/^(\d{4})-(\d{2})-(\d{2})$/.test(dateValue))throw Error('Enter a valid follow-up date.');
 if(typeof timeValue!=='string'||!/^(\d{2}):(\d{2})$/.test(timeValue))throw Error('Enter a valid follow-up time.');
 if(typeof zone!=='string'||!validZone(zone))throw Error('Choose a valid timezone.');
 const [year,month,day]=dateValue.split('-').map(Number),[hour,minute]=timeValue.split(':').map(Number);
 if(month<1||month>12||day<1||day>31||hour>23||minute>59)throw Error('Enter a valid follow-up date and time.');
 const target=Date.UTC(year,month-1,day,hour,minute,0);let instant=target;
 for(let i=0;i<3;i++){const p=zoneParts(new Date(instant),zone);const shown=Date.UTC(p.year,p.month-1,p.day,p.hour,p.minute,p.second);instant=target-(shown-instant)}
 const result=new Date(instant),p=zoneParts(result,zone);
 if(p.year!==year||p.month!==month||p.day!==day||p.hour!==hour||p.minute!==minute)throw Error('That local time does not exist in the selected timezone.');
 return result;
}
function scheduleInput(data,now){
 const timezone=String(data.timezone||'Asia/Kolkata').trim();
 const scheduled=zonedDateTime(String(data.date||''),String(data.time||''),timezone);
 if(scheduled.getTime()<=now())throw Error('Follow-up time must be in the future.');
 return {scheduledAt:scheduled.toISOString(),timezone};
}
function text(value,max,label,required=false){const result=String(value||'').trim();if(required&&!result)throw Error(`Enter ${label}.`);if(result.length>max)throw Error(`${label} is too long.`);if(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(result))throw Error(`${label} contains unsupported characters.`);return result}
function integer(value,fallback,min,max,label){const result=value==null||value===''?fallback:Number(value);if(!Number.isInteger(result)||result<min||result>max)throw Error(`${label} must be between ${min} and ${max}.`);return result}
function normalized(data,now){
 const customerName=text(data.customerName,80,'the customer name',true);
 if(!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(customerName))throw Error('Enter a valid customer name.');
 const phoneNumber=String(data.phoneNumber||'').replace(/[ ()-]/g,'');
 if(!/^\+91[6-9]\d{9}$/.test(phoneNumber))throw Error('Enter an Indian mobile number with +91.');
 const reason=text(data.reason,200,'a follow-up reason',true),notes=text(data.notes,2000,'notes');
 const agentId=text(data.agentId||'current',80,'the voice agent',true);
 if(!/^[A-Za-z0-9_.-]{1,80}$/.test(agentId))throw Error('Choose a valid voice agent.');
 return {customerName,phoneNumber,...scheduleInput(data,now),reason,notes,agentId,
  maxAttempts:integer(data.maxAttempts,3,1,10,'Maximum attempts'),
  retryIntervalMinutes:integer(data.retryIntervalMinutes,30,1,10080,'Retry interval')};
}
function createFollowUpStore(directory,{now=Date.now,logger=console}={}){
 const root=path.join(directory,'follow-ups'),lockFile=path.join(directory,'follow-up-scheduler.lock');
 const ownerKey=owner=>crypto.createHash('sha256').update(String(owner).toLowerCase()).digest('hex');
 const file=owner=>path.join(root,ownerKey(owner)+'.json');
 const log=(event,data={})=>logger.info?.(JSON.stringify({scope:'follow_up',event,at:new Date(now()).toISOString(),...data}));
 function read(owner){try{const data=JSON.parse(fs.readFileSync(file(owner),'utf8'));return Array.isArray(data.followUps)?data.followUps:[]}catch{return []}}
 function save(owner,records){fs.mkdirSync(root,{recursive:true,mode:0o700});const dest=file(owner),tmp=dest+'.'+process.pid+'.tmp';fs.writeFileSync(tmp,JSON.stringify({version:1,owner,followUps:records},null,2),{mode:0o600});fs.renameSync(tmp,dest)}
 function mutate(owner,change){const records=read(owner),result=change(records);save(owner,records);return result}
 const event=(record,status,message)=>{record.events=record.events||[];record.events.push({at:new Date(now()).toISOString(),status,message});if(record.events.length>100)record.events=record.events.slice(-100)};
 function build(owner,data,{source='dashboard',sourceRef=''}={}){
  const values=normalized(data,now),stamp=new Date(now()).toISOString();
  const sourceKey=crypto.createHash('sha256').update(`${values.phoneNumber}|${values.scheduledAt}|${values.reason.toLowerCase()}`).digest('hex');
  const record={id:crypto.randomUUID(),userId:owner,...values,status:'Scheduled',attemptCount:0,
   lastAttemptAt:null,nextAttemptAt:values.scheduledAt,airtelCallId:null,historyId:null,lastCallStatus:null,
   callSummary:null,callOutcome:null,customerResponse:null,failureReason:null,callStartedAt:null,
   callEndedAt:null,durationSeconds:null,recordingId:null,source,sourceRef:sourceRef||null,sourceKey,createdAt:stamp,updatedAt:stamp,events:[]};
  event(record,'Scheduled',source==='csv'?'Follow-up imported automatically from CSV.':'Follow-up created.');return record;
 }
 function create(owner,data,{source='dashboard'}={}){
  const record=build(owner,data,{source});mutate(owner,records=>records.push(record));
  log('created',{owner,id:record.id,scheduledAt:record.scheduledAt,source});return publicRecord(record);
 }
 function importBatch(owner,items,{source='csv'}={}){
  if(!Array.isArray(items)||!items.length)throw Error('No follow-up rows were found.');
  const records=read(owner),sourceRefs=new Set(records.filter(r=>r.source===source&&r.sourceRef).map(r=>String(r.sourceRef))),sourceKeys=new Set(records.map(r=>r.sourceKey||crypto.createHash('sha256').update(`${r.phoneNumber}|${r.scheduledAt}|${String(r.reason||'').toLowerCase()}`).digest('hex')));
  const imported=[],skipped=[];
  for(const item of items){try{
   const sourceRef=String(item.sourceRef||item.data?.sourceRef||'').trim();
   if(sourceRef&&sourceRefs.has(sourceRef))throw Error('This source record was already imported.');
   const record=build(owner,item.data||item,{source,sourceRef});
   if(sourceKeys.has(record.sourceKey))throw Error('This follow-up is already scheduled.');
   records.push(record);imported.push(publicRecord(record));sourceKeys.add(record.sourceKey);if(sourceRef)sourceRefs.add(sourceRef);
  }catch(e){skipped.push({row:item.row||null,error:e.message})}}
  if(imported.length)save(owner,records);
  log('batch_imported',{owner,source,imported:imported.length,skipped:skipped.length});
  return {imported,skipped,summary:{total:items.length,imported:imported.length,skipped:skipped.length}};
 }
 function publicRecord(record){const copy=JSON.parse(JSON.stringify(record));delete copy.lockToken;delete copy.sourceKey;return copy}
 function get(owner,id){return read(owner).find(r=>r.id===id)||null}
 function list(owner,{filter='All',search=''}={}){
  const query=String(search).trim().toLowerCase(),wanted=String(filter||'All').toLowerCase(),todayCache=new Map();
  return read(owner).filter(record=>{
   if(query&&!`${record.customerName} ${record.phoneNumber}`.toLowerCase().includes(query))return false;
   if(wanted==='all')return true;
   if(wanted==='upcoming')return ACTIVE.has(record.status)&&Date.parse(record.nextAttemptAt||record.scheduledAt)>now();
   if(wanted==='today'){const zone=record.timezone||'Asia/Kolkata';let current=todayCache.get(zone);if(!current){const p=zoneParts(new Date(now()),zone);current=`${p.year}-${String(p.month).padStart(2,'0')}-${String(p.day).padStart(2,'0')}`;todayCache.set(zone,current)}const p=zoneParts(new Date(record.nextAttemptAt||record.scheduledAt),zone);return current===`${p.year}-${String(p.month).padStart(2,'0')}-${String(p.day).padStart(2,'0')}`}
   return record.status.toLowerCase()===wanted;
  }).sort((a,b)=>Date.parse(a.nextAttemptAt||a.scheduledAt)-Date.parse(b.nextAttemptAt||b.scheduledAt)).map(publicRecord);
 }
 function edit(owner,id,data){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r)throw Error('Follow-up not found.');if(!EDITABLE.has(r.status))throw Error('This follow-up can no longer be edited.');const values=normalized({...r,date:'2099-01-01',time:'00:00',...data},now);delete values.scheduledAt;delete values.timezone;Object.assign(r,values,{updatedAt:new Date(now()).toISOString()});event(r,r.status,'Follow-up updated.');log('updated',{owner,id});return publicRecord(r)})}
 function reschedule(owner,id,data){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r)throw Error('Follow-up not found.');if(r.status==='Calling')throw Error('A call in progress cannot be rescheduled.');if(['Completed','Cancelled'].includes(r.status))throw Error('This follow-up cannot be rescheduled.');const values=scheduleInput(data,now);Object.assign(r,values,{status:'Rescheduled',attemptCount:0,lastAttemptAt:null,nextAttemptAt:values.scheduledAt,airtelCallId:null,historyId:null,lastCallStatus:'Rescheduled',failureReason:null,callStartedAt:null,callEndedAt:null,durationSeconds:null,recordingId:null,updatedAt:new Date(now()).toISOString()});event(r,'Rescheduled','Follow-up rescheduled.');log('rescheduled',{owner,id,scheduledAt:r.scheduledAt});return publicRecord(r)})}
 function cancel(owner,id){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r)throw Error('Follow-up not found.');if(r.status==='Calling')throw Error('This call is already in progress and the current Airtel integration cannot terminate it safely.');if(r.status==='Completed')throw Error('A completed follow-up cannot be cancelled.');r.status='Cancelled';r.nextAttemptAt=null;r.updatedAt=new Date(now()).toISOString();event(r,'Cancelled','Follow-up cancelled.');log('cancelled',{owner,id});return publicRecord(r)})}
 function claim(owner,id,{force=false}={}){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r)throw Error('Follow-up not found.');if(!ACTIVE.has(r.status)&&!(force&&['No Answer','Failed'].includes(r.status)))throw Error('This follow-up is not available to call.');if(!force&&Date.parse(r.nextAttemptAt||r.scheduledAt)>now())return null;const token=crypto.randomUUID(),stamp=new Date(now()).toISOString();Object.assign(r,{status:'Calling',attemptCount:r.attemptCount+1,lastAttemptAt:stamp,callStartedAt:stamp,nextAttemptAt:null,lockToken:token,updatedAt:stamp,failureReason:null});event(r,'Calling','Follow-up claimed for Airtel submission.');log('triggered',{owner,id,attempt:r.attemptCount});return {record:publicRecord(r),token}})}
 function submitted(owner,id,token,result){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r||r.lockToken!==token||r.status!=='Calling')return null;Object.assign(r,{airtelCallId:result.requestId||null,historyId:result.historyId||null,lastCallStatus:'Submitted',callSummary:result.message||null,updatedAt:new Date(now()).toISOString()});delete r.lockToken;event(r,'Calling','Airtel accepted the follow-up call request.');log('airtel_call_created',{owner,id,airtelCallId:r.airtelCallId,historyId:r.historyId});return publicRecord(r)})}
 function failed(owner,id,token,{message,status='Failed',retryable=false}={}){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r||r.status!=='Calling'||(token&&r.lockToken&&r.lockToken!==token))return null;delete r.lockToken;const canRetry=retryable&&r.attemptCount<r.maxAttempts;Object.assign(r,{status:canRetry?'Retry Scheduled':status,lastCallStatus:status,failureReason:message||null,callEndedAt:new Date(now()).toISOString(),nextAttemptAt:canRetry?new Date(now()+r.retryIntervalMinutes*60000).toISOString():null,updatedAt:new Date(now()).toISOString()});event(r,r.status,canRetry?'Retry scheduled.':message||'Follow-up failed.');log(canRetry?'retry_scheduled':'failed',{owner,id,attempt:r.attemptCount,nextAttemptAt:r.nextAttemptAt});return publicRecord(r)})}
 function completed(owner,id,call){return mutate(owner,records=>{const r=records.find(v=>v.id===id);if(!r||r.status!=='Calling')return null;delete r.lockToken;Object.assign(r,{status:'Completed',lastCallStatus:call.result||'Answered',callSummary:call.remarks||'Call completed.',callOutcome:call.result||'Answered',callEndedAt:new Date(now()).toISOString(),durationSeconds:call.duration??null,recordingId:call.recordingId||null,nextAttemptAt:null,updatedAt:new Date(now()).toISOString()});event(r,'Completed','Follow-up call completed.');log('completed',{owner,id,durationSeconds:r.durationSeconds});return publicRecord(r)})}
 function all(){if(!fs.existsSync(root))return [];return fs.readdirSync(root).filter(n=>/^[a-f0-9]{64}\.json$/.test(n)).flatMap(name=>{try{const payload=JSON.parse(fs.readFileSync(path.join(root,name),'utf8'));return (payload.followUps||[]).map(record=>({owner:payload.owner,record}))}catch{return []}})}
 async function schedulerLock(work){fs.mkdirSync(directory,{recursive:true});let handle;try{handle=fs.openSync(lockFile,'wx',0o600)}catch(e){if(e.code!=='EEXIST')throw e;try{if(now()-fs.statSync(lockFile).mtimeMs>60000)fs.unlinkSync(lockFile);else return false}catch{return false}handle=fs.openSync(lockFile,'wx',0o600)}try{fs.writeFileSync(handle,JSON.stringify({pid:process.pid,at:now()}));await work();return true}finally{try{fs.closeSync(handle)}catch{}try{fs.unlinkSync(lockFile)}catch{}}}
 return {STATUSES,create,importBatch,get,list,edit,reschedule,cancel,claim,submitted,failed,completed,all,schedulerLock,log};
}
module.exports={createFollowUpStore,zonedDateTime,validZone,STATUSES};
