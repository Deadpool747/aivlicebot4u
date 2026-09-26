const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
function headers(body,appId,key,date=new Date().toUTCString()){
 const digest='SHA-256='+crypto.createHash('sha256').update(body).digest('base64');
 const signature=crypto.createHmac('sha256',key).update('x-date: '+date+'\ndigest: '+digest).digest('base64');
 return {'Content-Type':'application/json','X-Date':date,Digest:digest,Authorization:'hmac username='+JSON.stringify(appId)+', algorithm="hmac-sha256", headers="x-date digest", signature='+JSON.stringify(signature)};
}
function createAirtelCaller({directory,value,readScript,history,fetcher=fetch}){
 const pending=new Set(),recent=new Map();
 return async(owner,data)=>{
  const fail=(status,error,uncertain=false,historyId)=>({status,body:{error,uncertain,historyId}});
  if(owner!=='huzaifa')return fail(403,'No Airtel number is assigned to this account.');
  if(!value('AIRTEL_APP_ID')||!value('AIRTEL_API_KEY'))return fail(503,'Airtel outbound API credentials are missing.');
  if(value('AIRTEL_OUTBOUND_ENABLED')!=='true')return fail(503,'Airtel outbound flow verification is pending.');
  const number=String(data.number||'').replace(/[ ()-]/g,''),name=String(data.name||'').trim();
  if(!/^\+91[6-9]\d{9}$/.test(number))return fail(400,'Enter an Indian mobile number with +91.');
  if(name&&!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(name))return fail(400,'Enter a valid client name.');
  if(!readScript(owner,'outbound').trim())return fail(400,'Save an outbound script first.');
  const now=Date.now(),key=owner+number;
  for(const [k,t] of recent)if(now-t>60000)recent.delete(k);
  if(pending.has(owner)||recent.has(key))return fail(429,'Wait before submitting another call to this number.');
  pending.add(owner);recent.set(key,now);
  let historyId;
  try{
   const id=crypto.randomUUID(),folder=path.join(directory,'airtel-pending');fs.mkdirSync(folder,{recursive:true});
   historyId=history?.add(owner,{kind:'request',type:'outbound',phone:number,name,result:'Pending',remarks:'Airtel call requested; answer not confirmed.'});
   const followUp=data.followUp&&typeof data.followUp==='object'?{
    id:String(data.followUp.id||'').slice(0,80),reason:String(data.followUp.reason||'').slice(0,200),
    notes:String(data.followUp.notes||'').slice(0,2000),scheduledAt:String(data.followUp.scheduledAt||'').slice(0,40),
    timezone:String(data.followUp.timezone||'Asia/Kolkata').slice(0,80),previousSummary:String(data.followUp.previousSummary||'').slice(0,1000)
   }:null;
   fs.writeFileSync(path.join(folder,id+'.json'),JSON.stringify({owner,direction:'outbound',number,name,historyId,followUp,expiresAt:now+360000}),{mode:0o600});
   const body=JSON.stringify({from:{number:number.slice(3),participant_name:'Customer',max_timeout_seconds:59},to:[{number:'+918045911978',participant_name:'Bot',max_timeout_seconds:59}],caller_id:'8045911978',metaData:{voicebotUrl:'wss://aivoicebot4u.com/airtel-iq/ws-airtel/',bot4u_request_id:id},call_flow_id:'e55a0f09-1c99-40a7-b0ff-041ef088447e'});
   const response=await fetcher('https://iqvoice.airtel.in/gateway/airtel-xchange/v2/adv/click-to-call',{method:'POST',headers:headers(body,value('AIRTEL_APP_ID'),value('AIRTEL_API_KEY')),body,signal:AbortSignal.timeout(20000)});
   if(!response.ok){
    const detail=await response.json().catch(()=>({}));
    const known={"No valid participant passed in 'to'. At least 1 valid participant required.":'Airtel rejected the call: no valid destination participant. The Airtel request format needs verification.',"JSON decoding error":'Airtel rejected the call request format.'};
    const message=known[detail.errorMessage]||'Airtel rejected the request (HTTP '+response.status+'). Check Airtel call logs.';
    if(response.status>=400&&response.status<500){
     fs.renameSync(path.join(folder,id+'.json'),path.join(folder,id+'.rejected'));
     if(historyId)history?.edit(owner,historyId,{result:'Not answered',remarks:message});
     return fail(502,message,false,historyId);
    }
    return fail(502,message,true,historyId);
   }
   return {status:202,body:{historyId,requestId:id,message:'Call submitted to Airtel; ringing and answer are not yet confirmed.'}};
  }catch{return fail(502,'Airtel call status is unknown. Check Airtel call logs before retrying.',true,historyId)}
  finally{pending.delete(owner)}
 };
}
module.exports={headers,createAirtelCaller};
