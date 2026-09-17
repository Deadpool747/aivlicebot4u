const fs=require('node:fs'),path=require('node:path');
function createPhoneCaller({directory,token,readScript,fetcher=fetch,history}){
 const attempts=new Map(),pending=new Set();
 return async function call(owner,data){
  const fail=(status,error)=>({status,body:{error}});
  const number=String(data.number||'').replace(/[ ()-]/g,'');
  if(!/^\+[1-9]\d{6,14}$/.test(number))return fail(400,'Enter a phone number with + and country code.');
  const mapping=JSON.parse(fs.readFileSync(path.join(directory,'telephony.json'),'utf8'))[owner];
  if(!mapping)return fail(403,'No phone number is assigned to this account.');
  if(!token())return fail(503,'Piopiy is not configured.');
  if(!readScript(owner,'outbound').trim())return fail(400,'Save an outbound script first.');
  let worker;try{worker=JSON.parse(fs.readFileSync(path.join(directory,'phone-worker.json'),'utf8'))}catch{}
  if(!worker||worker.owner!==owner||worker.agent_id!==mapping.agent_id||Date.now()-worker.updated_at>20000||!worker.connected)return fail(503,'The phone worker is offline. Start it before calling.');
  const now=Date.now(),attemptKey=owner+':'+number;
  for(const [key,time] of attempts)if(now-time>=60000)attempts.delete(key);
  if(pending.has(owner))return fail(429,'Your previous call request is still being submitted. Please wait.');
  const previous=attempts.get(attemptKey);
  if(previous)return fail(429,'A call to this number was recently requested. Wait '+Math.ceil((60000-(now-previous))/1000)+' seconds before retrying this number.');
  const name=typeof data.name==='string'?data.name.trim():'';
  if(name&&!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(name))return fail(400,'Enter a valid client name.');
  attempts.set(attemptKey,now);pending.add(owner);
  const historyId=history?.add(owner,{kind:'request',phone:number,name,result:'Pending',remarks:'Call requested. Carrier answer status is not yet confirmed.'});
  try{
   const r=await fetcher('https://rest.piopiy.com/v3/voice/ai/call',{method:'POST',headers:{Authorization:'Bearer '+token(),'Content-Type':'application/json'},signal:AbortSignal.timeout(20000),body:JSON.stringify({caller_id:mapping.caller_id,to_number:number.slice(1),agent_id:mapping.agent_id,options:{max_duration_sec:300,ring_timeout_sec:60},variables:{customer_name:name,call_mode:'outbound',history_id:historyId||''}})});
   const body=await r.json();
   if(!r.ok||body.error||body.success===false||(body.code&&!['200',200,'cmi-200'].includes(body.code)))return fail(502,'Piopiy did not accept the call. Check your Piopiy balance and account settings.');
   if(body.message!=='call_queued'||typeof body.request!=='string')return fail(502,'Piopiy returned an unexpected response. Check call history before retrying.');
   return {status:202,body:{requestId:body.request,message:'Piopiy queued the call; ringing is not confirmed. Request ID: '+body.request}};
  }catch{return fail(502,'Call status could not be confirmed. Check Piopiy call history before trying again.');}
  finally{pending.delete(owner);}
 };
}
module.exports={createPhoneCaller};
