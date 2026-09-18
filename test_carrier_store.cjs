const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {createCarrierStore}=require('./carrier-store.cjs');
test('carrier changes persist and preserve other accounts and numbers',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'carriers-'));
 try{fs.writeFileSync(path.join(dir,'telephony.json'),JSON.stringify({huzaifa:{caller_id:'917943444692'},other:{caller_id:'123'}}));
 const s=createCarrierStore(dir);s.select('huzaifa','airtel_iq');assert.equal(createCarrierStore(dir).read('huzaifa').provider,'airtel_iq');
 s.select('huzaifa','piopiy');assert.equal(s.read('huzaifa').outbound_provider,'piopiy');assert.equal(s.read('huzaifa').caller_id,'917943444692');assert.deepEqual(s.read('other'),{caller_id:'123'});
 assert.throws(()=>s.select('other','airtel_iq'));assert.throws(()=>s.select('huzaifa','bad'));
 }finally{fs.rmSync(dir,{recursive:true,force:true})}
});
