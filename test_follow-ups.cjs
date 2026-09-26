const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {createFollowUpStore,zonedDateTime}=require('./follow-up-store.cjs');
const {createFollowUpScheduler}=require('./follow-up-scheduler.cjs');
const {parseFollowUpCsv}=require('./follow-up-import.cjs');

const silent={info(){}};
const base=()=>({customerName:'Priya Shah',phoneNumber:'+919876543210',date:'2026-01-02',time:'10:00',timezone:'Asia/Kolkata',reason:'Discuss the test drive',notes:'Asked for a morning call',agentId:'current',maxAttempts:3,retryIntervalMinutes:30});

async function main(){
 assert.equal(zonedDateTime('2026-01-02','10:00','Asia/Kolkata').toISOString(),'2026-01-02T04:30:00.000Z');
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-follow-up-'));let clock=Date.parse('2026-01-01T00:00:00Z');
 const store=createFollowUpStore(root,{now:()=>clock,logger:silent});
 const csv='\uFEFFcustomer_name,phone_number,follow_up_date,follow_up_time,timezone,reason,notes,source_ref\r\n"Asha Rao",+919123456789,2026-01-02,12:00,Asia/Kolkata,"Discuss, sedan",Ready,lead-1\r\nBad Phone,123,2026-01-02,13:00,Asia/Kolkata,Callback,,lead-2';
 const parsed=parseFollowUpCsv(csv);assert.equal(parsed.length,2);assert.equal(parsed[0].data.reason,'Discuss, sedan');
 const batch=store.importBatch('huzaifa',parsed,{source:'csv'});assert.equal(batch.summary.imported,1);assert.equal(batch.summary.skipped,1);assert.match(batch.skipped[0].error,/Indian mobile/);
 const repeated=store.importBatch('huzaifa',parsed,{source:'csv'});assert.equal(repeated.summary.imported,0);assert.equal(repeated.summary.skipped,2);
 assert.equal(store.list('huzaifa').find(item=>item.sourceRef==='lead-1').source,'csv');
 assert.throws(()=>parseFollowUpCsv('name,phone\nA,+919123456789'),/missing required columns/);
 assert.throws(()=>store.create('huzaifa',{...base(),date:'2025-12-31'}),/future/);
 const record=store.create('huzaifa',base());assert.equal(record.status,'Scheduled');assert.equal(record.scheduledAt,'2026-01-02T04:30:00.000Z');
 assert.equal(store.list('another').length,0);assert.equal(store.list('huzaifa',{search:'9876'}).length,1);
 const edited=store.edit('huzaifa',record.id,{reason:'Updated reason'});assert.equal(edited.reason,'Updated reason');assert.equal(edited.scheduledAt,record.scheduledAt);
 const rescheduled=store.reschedule('huzaifa',record.id,{date:'2026-01-03',time:'11:00',timezone:'Asia/Kolkata'});assert.equal(rescheduled.status,'Rescheduled');
 const claim=store.claim('huzaifa',record.id,{force:true});assert.equal(claim.record.attemptCount,1);assert.throws(()=>store.claim('huzaifa',record.id,{force:true}),/not available/);
 store.failed('huzaifa',record.id,claim.token,{message:'No answer',status:'No Answer',retryable:true});assert.equal(store.get('huzaifa',record.id).status,'Retry Scheduled');
 const retryAt=Date.parse(store.get('huzaifa',record.id).nextAttemptAt);assert.equal(retryAt,clock+30*60000);

 const calls=[];const historyRows=[];const scheduler=createFollowUpScheduler({store,history:{list:()=>historyRows},call:async(owner,data)=>{calls.push({owner,data});return {status:202,body:{historyId:'history-1',requestId:'airtel-1',message:'accepted'}}},now:()=>clock,logger:silent,autostart:false});
 clock=retryAt;await scheduler.tick();assert.equal(calls.length,1);assert.equal(calls[0].data.followUp.reason,'Updated reason');assert.equal(store.get('huzaifa',record.id).status,'Calling');
 historyRows.push({id:'history-1',result:'Answered',duration:42,remarks:'Interested',recordingId:'recording-1'});scheduler.reconcile();const done=store.get('huzaifa',record.id);assert.equal(done.status,'Completed');assert.equal(done.durationSeconds,42);

 const interrupted=store.create('huzaifa',{...base(),date:'2026-01-04'});store.claim('huzaifa',interrupted.id,{force:true});clock+=61000;scheduler.reconcile();assert.equal(store.get('huzaifa',interrupted.id).status,'Failed');
 const exhausted=store.create('huzaifa',{...base(),date:'2026-01-04',maxAttempts:1});const finalClaim=store.claim('huzaifa',exhausted.id,{force:true});store.failed('huzaifa',exhausted.id,finalClaim.token,{message:'No answer',status:'No Answer',retryable:true});assert.equal(store.get('huzaifa',exhausted.id).status,'No Answer');assert.equal(store.get('huzaifa',exhausted.id).nextAttemptAt,null);
 const cancel=store.create('huzaifa',{...base(),date:'2026-01-05'});assert.equal(store.cancel('huzaifa',cancel.id).status,'Cancelled');
 scheduler.close();fs.rmSync(root,{recursive:true,force:true});console.log('follow-up tests passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
