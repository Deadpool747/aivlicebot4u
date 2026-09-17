const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
function createScriptStore(directory){
function file(username,mode){if(!username||!['inbound','outbound'].includes(mode))throw Error('Invalid script owner or mode');const id=crypto.createHash('sha256').update(username.toLowerCase()).digest('hex');return path.join(directory,id,mode+'.txt')}
return {read(username,mode){const p=file(username,mode);return fs.existsSync(p)?fs.readFileSync(p,'utf8'):''},write(username,mode,script){const p=file(username,mode);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p+'.tmp',script,'utf8');fs.renameSync(p+'.tmp',p)},migrate(accounts,defaults){const marker=path.join(directory,'.migrated');if(fs.existsSync(marker))return;for(const user of accounts)for(const mode of ['inbound','outbound'])if(!fs.existsSync(file(user.username,mode)))this.write(user.username,mode,defaults[mode]);fs.mkdirSync(directory,{recursive:true});fs.writeFileSync(marker,'1')}};
}
module.exports={createScriptStore};
