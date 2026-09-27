const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {createHistory}=require('./call-history.cjs');

test('follow-up attempt persists independently from remarks',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),id=history.add('huzaifa',{kind:'request',type:'outbound',phone:'+919876543210',name:'Rahul',result:'Answered',remarks:'Follow up'});
  assert.equal(history.edit('huzaifa',id,{followUpAttempt:2}),true);
  let row=history.list('huzaifa').find(call=>call.id===id);assert.equal(row.followUpAttempt,2);assert.equal(row.remarks,'Follow up');
  history.edit('huzaifa',id,{remarks:'Interested',result:''});row=history.list('huzaifa').find(call=>call.id===id);assert.equal(row.followUpAttempt,2);assert.equal(row.remarks,'Interested');
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('a Follow up remark automatically enters Attempt 1',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),id=history.add('huzaifa',{kind:'request',type:'outbound',phone:'+919876543210',name:'Rahul',result:'Answered',remarks:'Follow up'});
  assert.equal(history.list('huzaifa').find(call=>call.id===id).followUpAttempt,1);
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('not answered and uncertain calls automatically enter Attempt 1',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),missed=history.add('huzaifa',{kind:'request',type:'outbound',phone:'+919876543210',result:'Not answered',remarks:'Voicemail detected'}),uncertain=history.add('huzaifa',{kind:'request',type:'outbound',phone:'+919876543211',result:'Pending',remarks:'Submitted'});
  const old=Date.now;Date.now=()=>old()+361000;try{const rows=history.list('huzaifa');assert.equal(rows.find(call=>call.id===missed).followUpAttempt,1);assert.equal(rows.find(call=>call.id===uncertain).result,'Unconfirmed');assert.equal(rows.find(call=>call.id===uncertain).followUpAttempt,1)}finally{Date.now=old}
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('a fresh carrier request is hidden until the ring timeout, then enters Attempt 1',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),id=history.add('huzaifa',{kind:'request',type:'outbound',phone:'+919876543210',result:'Pending',remarks:'Call submitted'});
  assert.equal(history.publicList('huzaifa').some(call=>call.id===id),false);
  const old=Date.now;Date.now=()=>old()+76000;try{const call=history.publicList('huzaifa').find(row=>row.id===id);assert.equal(call.result,'Not answered');assert.equal(call.followUpAttempt,1)}finally{Date.now=old}
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('repeated unanswered calls move a client through attempts and then junk without stale rows',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),phone='+919876543210';
  const first=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',remarks:'Voicemail detected'});
  const second=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',remarks:'No pickup'});
  let rows=history.list('huzaifa');assert.equal(rows.find(call=>call.id===first).followUpAttempt,undefined);assert.equal(rows.find(call=>call.id===second).followUpAttempt,2);
  const third=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',remarks:'Voicemail detected'});
  rows=history.list('huzaifa');assert.equal(rows.find(call=>call.id===second).followUpAttempt,undefined);assert.equal(rows.find(call=>call.id===third).followUpAttempt,3);
  const fourth=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',remarks:'No pickup'});
  rows=history.list('huzaifa');assert.equal(rows.find(call=>call.id===third).followUpAttempt,undefined);assert.equal(rows.find(call=>call.id===fourth).followUpAttempt,4);
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('scheduler attempt count is preserved and an answered call clears the follow-up queue',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),phone='+919876543210';
  const first=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',followUpAttempt:1});
  const second=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',followUpAttempt:2});
  let rows=history.list('huzaifa');assert.equal(rows.find(call=>call.id===first).followUpAttempt,undefined);assert.equal(rows.find(call=>call.id===second).followUpAttempt,2);
  history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Answered'});rows=history.list('huzaifa');assert.equal(rows.some(call=>call.followUpAttempt),false);
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('three scheduler retries after an initial missed call end in junk',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root),phone='+919876543210';
  history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered'});
  history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',followUpAttempt:1});
  history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',followUpAttempt:2});
  const final=history.add('huzaifa',{kind:'request',type:'outbound',phone,result:'Not answered',followUpAttempt:3});
  const rows=history.list('huzaifa');assert.equal(rows.find(call=>call.id===final).followUpAttempt,4);assert.equal(rows.filter(call=>call.followUpAttempt).length,1);
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('public call results contain only Answered or Not answered',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root);history.add('huzaifa',{kind:'request',result:'Pending'});history.add('huzaifa',{kind:'request',result:'Answered'});history.add('huzaifa',{kind:'request',result:'Unconfirmed'});assert.deepEqual(new Set(history.publicList('huzaifa').map(call=>call.result)),new Set(['Answered','Not answered']))}
 finally{fs.rmSync(root,{recursive:true,force:true})}
});
