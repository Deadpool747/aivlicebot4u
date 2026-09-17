const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {promisify}=require('node:util');const scrypt=promisify(crypto.scrypt);
function createAuth(directory){
const file=path.join(directory,'account.json'),sessions=new Map();let creating=false,attempts=[];
const usersFile=path.join(directory,'users.json');
function users(){if(fs.existsSync(usersFile))return JSON.parse(fs.readFileSync(usersFile,'utf8'));return fs.existsSync(file)?[JSON.parse(fs.readFileSync(file,'utf8'))]:[]}
function saveUsers(records){fs.mkdirSync(directory,{recursive:true});fs.writeFileSync(usersFile+'.tmp',JSON.stringify(records),{mode:0o600});fs.renameSync(usersFile+'.tmp',usersFile)}
const token=req=>(req.headers.cookie||'').split(';').map(x=>x.trim()).find(x=>x.startsWith('voice_session='))?.slice(14);
const session=req=>{const id=token(req),s=sessions.get(id);if(!s||s.expires<Date.now()){sessions.delete(id);return null}return s};
const json=(res,code,data)=>{res.writeHead(code,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data))};
async function handle(req,res){
if(req.url==='/api/auth/status'&&req.method==='GET'){json(res,200,{setupRequired:users().length===0,authenticated:!!session(req),username:session(req)?.username||null});return true}
if(!['/api/auth/setup','/api/auth/signup','/api/auth/login','/api/auth/logout'].includes(req.url))return false;
if(req.method!=='POST'){json(res,405,{error:'Method not allowed'});return true}
if(req.url.endsWith('/logout')){const s=session(req);if(s)for(const socket of s.sockets)socket.close(1000,'Signed out');sessions.delete(token(req));res.setHeader('Set-Cookie','voice_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0');json(res,200,{ok:true});return true}
if(!req.headers['content-type']?.startsWith('application/json')){json(res,415,{error:'JSON required'});return true}
attempts=attempts.filter(t=>Date.now()-t<60000);if(attempts.length>=10){json(res,429,{error:'Too many attempts. Wait a minute and try again.'});return true}attempts.push(Date.now());
let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>8192){json(res,413,{error:'Request too large'});return true}}
let data;try{data=JSON.parse(body)}catch{json(res,400,{error:'Invalid request'});return true}
const username=typeof data.username==='string'?data.username.trim():'';const password=data.password;
if(!/^[a-zA-Z0-9_.-]{3,40}$/.test(username)||typeof password!=='string'||password.length<12||password.length>128){json(res,400,{error:'Use a username of 3–40 letters, numbers, dots, hyphens or underscores and a password of 12–128 characters.'});return true}
if(req.url.endsWith('/setup')||req.url.endsWith('/signup')){
if(creating){json(res,409,{error:'Another account is being created. Please try again.'});return true}creating=true;
try{const records=users();if((req.url.endsWith('/setup')&&records.length)||records.some(u=>u.username.toLowerCase()===username.toLowerCase())){json(res,409,{error:'This username is already taken. Sign in or choose another username.'});return true}
const salt=crypto.randomBytes(16).toString('hex'),hash=(await scrypt(password,salt,64)).toString('hex');saveUsers([...records,{username,salt,hash}]);}finally{creating=false}
}else{
const user=users().find(u=>u.username.toLowerCase()===username.toLowerCase());const hash=await scrypt(password,user?.salt||'unknown-user-dummy-salt',64);if(!user||!crypto.timingSafeEqual(hash,Buffer.from(user.hash,'hex'))){json(res,401,{error:'Incorrect username or password.'});return true}
}

sessions.delete(token(req));const id=crypto.randomBytes(32).toString('hex');sessions.set(id,{username:username.toLowerCase(),expires:Date.now()+8*3600000,sockets:new Set()});res.setHeader('Set-Cookie',`voice_session=${id}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800`);json(res,200,{ok:true});return true;
}
return {handle,session};
}
module.exports={createAuth};
