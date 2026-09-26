const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const digest=token=>crypto.createHash('sha256').update(token).digest();
function createApiKeys(directory,{userExists,now=Date.now}={}){
 const folder=path.join(directory,'api-keys');
 const file=id=>path.join(folder,id+'.json');
 const read=id=>{try{return JSON.parse(fs.readFileSync(file(id),'utf8'))}catch{return null}};
 function save(record){fs.mkdirSync(folder,{recursive:true,mode:0o700});fs.writeFileSync(file(record.id)+'.tmp',JSON.stringify(record),{mode:0o600});fs.renameSync(file(record.id)+'.tmp',file(record.id))}
 const publicRecord=({id,name,prefix,createdAt,lastUsedAt,revokedAt})=>({id,name,prefix,createdAt,lastUsedAt,revokedAt,status:revokedAt?'Revoked':'Active'});
 function records(owner){return fs.existsSync(folder)?fs.readdirSync(folder).filter(f=>/^[a-f0-9]{32}\.json$/.test(f)).map(f=>read(f.slice(0,-5))).filter(r=>r&&r.owner===owner):[]}
 return {
 list:owner=>records(owner).map(publicRecord).sort((a,b)=>b.createdAt.localeCompare(a.createdAt)),
 create(owner,name){if(typeof name!=='string'||!name.trim()||name.trim().length>80||/[\x00-\x1f\x7f]/.test(name))throw Error('Enter a key name between 1 and 80 characters.');if(!userExists(owner))throw Error('Account unavailable.');if(records(owner).filter(r=>!r.revokedAt).length>=20)throw Error('Revoke an existing key before creating more than 20 active keys.');const id=crypto.randomBytes(16).toString('hex'),token='bot4u_live_'+id+'_'+crypto.randomBytes(32).toString('base64url');const record={id,owner,name:name.trim(),hash:digest(token).toString('hex'),prefix:token.slice(0,18),createdAt:new Date(now()).toISOString(),lastUsedAt:null,revokedAt:null};save(record);return {key:publicRecord(record),token}},
 revoke(owner,id){if(!/^[a-f0-9]{32}$/.test(id))return false;const r=read(id);if(!r||r.owner!==owner)return false;if(!r.revokedAt){r.revokedAt=new Date(now()).toISOString();save(r)}return true},
 verify(token){const match=/^bot4u_live_([a-f0-9]{32})_[A-Za-z0-9_-]{43}$/.exec(token||'');if(!match)return null;const r=read(match[1]);const expected=r&&/^[a-f0-9]{64}$/.test(r.hash)?Buffer.from(r.hash,'hex'):Buffer.alloc(32);if(!crypto.timingSafeEqual(digest(token),expected)||!r||r.revokedAt||!userExists(r.owner))return null;if(!r.lastUsedAt||now()-Date.parse(r.lastUsedAt)>=60000){r.lastUsedAt=new Date(now()).toISOString();save(r)}return {username:r.owner,keyId:r.id}}
 };
}
function createLimiter({limit=120,windowMs=60000,capacity=10000,now=Date.now}={}){const entries=new Map();return id=>{const time=now();for(const [k,v] of entries)if(v.until<=time)entries.delete(k);let entry=entries.get(id);if(!entry){if(entries.size>=capacity)return false;entry={count:0,until:time+windowMs};entries.set(id,entry)}return ++entry.count<=limit}}
const routes={
 '/api/v1/calls':{path:'/api/phone/call',methods:['POST']},
 '/api/v1/call-history':{path:'/api/call-history',methods:['GET','PATCH']},
 '/api/v1/recording':{path:'/api/call-recording',methods:['GET','HEAD']},
 '/api/v1/scripts':{path:'/api/script',methods:['GET','PUT']},
 '/api/v1/telephony':{path:'/api/account/telephony',methods:['GET','PUT']},
 '/api/v1/campaign':{path:'/api/phone/campaign',methods:['GET','POST']},
 '/api/v1/follow-ups/import':{path:'/api/follow-ups/import',methods:['POST']}
};
function createApiGateway(store){const byIp=createLimiter({limit:180}),byOwner=createLimiter({limit:120});return(req)=>{if(!byIp(req.socket.remoteAddress||'unknown'))return {status:429,error:'Too many API requests. Retry in one minute.'};const header=req.headers.authorization;if(!header)return {status:401,error:'API key required'};const match=/^Bearer ([^\s]+)$/i.exec(header);const identity=match&&store.verify(match[1]);if(!identity)return {status:401,error:'Invalid or revoked API key'};if(!byOwner(identity.username))return {status:429,error:'Too many API requests. Retry in one minute.'};const url=new URL(req.url,'http://localhost'),route=routes[url.pathname];if(!route)return {status:404,error:'API endpoint not found'};if(!route.methods.includes(req.method))return {status:405,error:'Method not allowed'};return {identity,url:route.path+url.search}}}
module.exports={createApiKeys,createApiGateway,createLimiter,routes};
