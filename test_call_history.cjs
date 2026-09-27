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

test('public call results contain only Answered or Not answered',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-history-'));
 try{const history=createHistory(root);history.add('huzaifa',{kind:'request',result:'Pending'});history.add('huzaifa',{kind:'request',result:'Answered'});history.add('huzaifa',{kind:'request',result:'Unconfirmed'});assert.deepEqual(new Set(history.publicList('huzaifa').map(call=>call.result)),new Set(['Answered','Not answered']))}
 finally{fs.rmSync(root,{recursive:true,force:true})}
});
