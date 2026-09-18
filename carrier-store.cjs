const fs=require('node:fs'),path=require('node:path');
function createCarrierStore(directory){
 const file=path.join(directory,'telephony.json');
 function read(owner){const all=JSON.parse(fs.readFileSync(file,'utf8'));return all[owner]||null}
 function select(owner,provider){
  if(!['piopiy','airtel_iq'].includes(provider))throw Error('Choose Piopiy or Airtel.');
  const all=JSON.parse(fs.readFileSync(file,'utf8'));
  if(!all[owner])throw Error('No phone number is assigned to this account.');
  if(provider==='airtel_iq'&&owner!=='huzaifa')throw Error('No Airtel number is assigned to this account.');
  all[owner].outbound_provider=provider;all[owner].provider=provider;
  fs.writeFileSync(file+'.tmp',JSON.stringify(all,null,2),{mode:0o600});fs.renameSync(file+'.tmp',file);
  return read(owner);
 }
 return {read,select};
}
module.exports={createCarrierStore};
