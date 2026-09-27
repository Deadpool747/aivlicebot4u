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
