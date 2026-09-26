function createFollowUpScheduler({store,call,history,intervalMs=2000,now=Date.now,logger=console,autostart=true}){
 let running=false;
 const log=(event,data={})=>logger.info?.(JSON.stringify({scope:'follow_up_scheduler',event,at:new Date(now()).toISOString(),...data}));
 async function submit(owner,id,{force=false}={}){
  let claimed;try{claimed=store.claim(owner,id,{force})}catch(error){return {status:409,body:{error:error.message}}}
  if(!claimed)return {status:409,body:{error:'This follow-up is not due yet.'}};
  const {record,token}=claimed;
  let result;try{result=await call(owner,{number:record.phoneNumber,name:record.customerName,followUp:{id:record.id,reason:record.reason,notes:record.notes,scheduledAt:record.scheduledAt,timezone:record.timezone,previousSummary:record.callSummary||''}})}catch{result={status:502,body:{error:'Airtel call submission failed unexpectedly.',uncertain:true}}}
  if(result.status===202){store.submitted(owner,id,token,result.body);return result}
  const uncertain=!!result.body?.uncertain,permanent=[400,403,503].includes(result.status)||/rejected|credentials|permission|assigned/i.test(result.body?.error||'');
  store.failed(owner,id,token,{message:result.body?.error||'Airtel call submission failed.',retryable:!uncertain&&!permanent});
  log(uncertain?'submission_uncertain':'submission_failed',{owner,id,status:result.status});return result;
 }
 function reconcile(){for(const {owner,record} of store.all()){
  if(record.status!=='Calling')continue;
  if(!record.historyId){if(Date.parse(record.lastAttemptAt||0)<now()-60000)store.failed(owner,record.id,null,{message:'Call submission was interrupted before an Airtel reference was stored. Check Airtel logs before retrying.'});continue}
  const callRow=history.list(owner).find(row=>row.id===record.historyId);
  if(!callRow)continue;
  if(callRow.result==='Answered'&&callRow.duration!==null)store.completed(owner,record.id,callRow);
  else if(callRow.result==='Not answered')store.failed(owner,record.id,null,{message:callRow.remarks||'Customer did not answer.',status:'No Answer',retryable:true});
  else if(callRow.result==='Unconfirmed')store.failed(owner,record.id,null,{message:'Airtel did not provide a confirmed final status. Review Airtel logs before using Call Now.',status:'Failed',retryable:false});
 }}
 async function tick(){if(running)return false;running=true;try{return await store.schedulerLock(async()=>{reconcile();const due=store.all().filter(({record})=>['Scheduled','Retry Scheduled','Rescheduled'].includes(record.status)&&Date.parse(record.nextAttemptAt||record.scheduledAt)<=now()).sort((a,b)=>Date.parse(a.record.nextAttemptAt||a.record.scheduledAt)-Date.parse(b.record.nextAttemptAt||b.record.scheduledAt));for(const item of due)await submit(item.owner,item.record.id)})}catch(error){log('scheduler_failure',{error:error.constructor.name,message:String(error.message||'').slice(0,300)});return false}finally{running=false}}
 async function callNow(owner,id){if(running)return {status:409,body:{error:'The follow-up scheduler is busy. Try again in a moment.'}};running=true;try{let response={status:409,body:{error:'Another follow-up scheduler is active. Try again in a moment.'}};await store.schedulerLock(async()=>{reconcile();response=await submit(owner,id,{force:true})});return response}finally{running=false}}
 const timer=autostart?setInterval(()=>tick(),intervalMs):null;if(timer)timer.unref();
 return {tick,callNow,reconcile,close:()=>timer&&clearInterval(timer)};
}
module.exports={createFollowUpScheduler};
