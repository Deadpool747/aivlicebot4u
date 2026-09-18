const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {createPhoneCaller}=require('./phone-call.cjs');
test('Airtel accounts cannot fall back to Piopiy for manual or CSV calls',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-provider-'));
 try{
  fs.writeFileSync(path.join(directory,'telephony.json'),JSON.stringify({huzaifa:{outbound_provider:'airtel_iq',agent_id:'existing-piopiy-agent'}}));
  let requested=false;
  const call=createPhoneCaller({directory,token:()=>{throw Error('Piopiy credentials must not be read')},readScript:()=>'',fetcher:async()=>{requested=true;throw Error('Unexpected carrier request')}});
  const result=await call('huzaifa',{number:'+919890267326'});
  assert.equal(result.status,503);
  assert.match(result.body.error,/Airtel outbound calling/);
  assert.equal(requested,false);
 }finally{fs.rmSync(directory,{recursive:true,force:true})}
});
