const http=require('node:http');
const fs=require('node:fs');
const path=require('node:path');
const sourceEnv=path.join(__dirname,'.env');
function value(name){if(process.env[name])return process.env[name];if(!fs.existsSync(sourceEnv))return '';const line=fs.readFileSync(sourceEnv,'utf8').split(/\r?\n/).find(line=>line.trim().startsWith(name+'='));return line?line.slice(line.indexOf('=')+1).trim().replace(/^(['"])(.*)\1$/,'$2'):''}
const key=process.env.GEMINI_API_KEY||value('GEMINI_API_KEY');
const {WebSocket,WebSocketServer}=require('ws');
const {createBookingMailer,validEmail}=require('./booking-email.cjs');
const model=value('GEMINI_LIVE_MODEL')||'gemini-3.1-flash-live-preview';
const promptPaths={inbound:path.join(__dirname,'sales-prompt.txt'),outbound:path.join(__dirname,'outbound-prompt.txt')};
const basePath=value('BASE_PATH').replace(/\/$/,'');
if(basePath&&!/^\/[a-zA-Z0-9_-]+$/.test(basePath))throw Error('Invalid BASE_PATH');
const publicOrigin=value('PUBLIC_ORIGIN');
if(publicOrigin&&new URL(publicOrigin).origin!==publicOrigin)throw Error('PUBLIC_ORIGIN must be an origin');
const auth=require('./auth.cjs').createAuth(path.join(__dirname,'.local'),{cookiePath:basePath||'/',secure:publicOrigin.startsWith('https://'),cookieName:basePath?'bot4u_session':'voice_session'});
const apiKeys=require('./api-keys.cjs').createApiKeys(path.join(__dirname,'.local'),{userExists:auth.userExists});
const apiGateway=require('./api-keys.cjs').createApiGateway(apiKeys);
const keyManagementLimit=require('./api-keys.cjs').createLimiter({limit:30});
const scripts=require('./script-store.cjs').createScriptStore(path.join(__dirname,'.local','scripts'));
const accountsPath=path.join(__dirname,'.local','users.json'),legacyAccountPath=path.join(__dirname,'.local','account.json');
const existingAccounts=fs.existsSync(accountsPath)?JSON.parse(fs.readFileSync(accountsPath,'utf8')):fs.existsSync(legacyAccountPath)?[JSON.parse(fs.readFileSync(legacyAccountPath,'utf8'))]:[];
scripts.migrate(existingAccounts,{inbound:fs.readFileSync(promptPaths.inbound,'utf8'),outbound:fs.readFileSync(promptPaths.outbound,'utf8')});
const port=Number(process.env.PORT||4174);
const history=require('./call-history.cjs').createHistory(path.join(__dirname,'.local','call-history'));
const airtelCall=require('./airtel-call.cjs').createAirtelCaller({directory:path.join(__dirname,'.local'),value,history,readScript:(owner,mode)=>scripts.read(owner,mode)});
const phoneCall=require('./phone-call.cjs').createPhoneCaller({airtelCall,history,directory:path.join(__dirname,'.local'),token:()=>value('PIOPIY_API_TOKEN'),readScript:(owner,mode)=>scripts.read(owner,mode)});
const carriers=require('./carrier-store.cjs').createCarrierStore(path.join(__dirname,'.local'));
const campaigns=require('./csv-calls.cjs').createCampaigns({directory:path.join(__dirname,'.local','campaigns'),call:phoneCall,history});
const followUps=require('./follow-up-store.cjs').createFollowUpStore(path.join(__dirname,'.local'));
const {parseFollowUpCsv}=require('./follow-up-import.cjs');
const followUpScheduler=require('./follow-up-scheduler.cjs').createFollowUpScheduler({store:followUps,call:airtelCall,history});
const internalTokenPath=path.join(__dirname,'.local','follow-up-internal-token');
fs.mkdirSync(path.dirname(internalTokenPath),{recursive:true});
if(!fs.existsSync(internalTokenPath))try{fs.writeFileSync(internalTokenPath,require('node:crypto').randomBytes(32).toString('hex'),{mode:0o600,flag:'wx'})}catch(e){if(e.code!=='EEXIST')throw e}
const internalToken=fs.readFileSync(internalTokenPath,'utf8').trim();
const hosts=[`127.0.0.1:${port}`,`localhost:${port}`,...(publicOrigin?[new URL(publicOrigin).host]:[])];
const origins=[`http://127.0.0.1:${port}`,`http://localhost:${port}`,...(publicOrigin?[publicOrigin]:[])];
function stripBase(req){if(!basePath)return true;if(!req.url.startsWith(basePath+'/'))return false;req.url=req.url.slice(basePath.length);return true}
const assets={'/numbers':'numbers.html','/numbers.js':'numbers.js','/':'index.html','/app.js':'app.js','/csv-calls.js':'csv-calls.js','/style.css':'style.css','/capture.js':'capture.js','/dashboard':'dashboard.html','/dashboard.js':'dashboard.js','/api-keys':'api-keys.html','/api-keys.js':'api-keys.js','/api-docs':'api-docs.html'};
function json(res,status,data){res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data))}
async function jsonBody(req,limit=8000){if(!req.headers['content-type']?.startsWith('application/json')){const error=Error('JSON required');error.status=415;throw error}let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>limit){const error=Error('Request too large');error.status=413;throw error}}try{return JSON.parse(body)}catch{const error=Error('Invalid JSON');error.status=400;throw error}}
function safeToken(value){const supplied=Buffer.from(String(value||'')),expected=Buffer.from(internalToken);return supplied.length===expected.length&&require('node:crypto').timingSafeEqual(supplied,expected)}
function requireAirtel(owner){const carrier=carriers.read(owner);if((carrier?.outbound_provider||carrier?.provider)!=='airtel_iq')throw Error('Select Airtel IQ as this account’s outbound carrier before scheduling follow-ups.')}
const server=http.createServer(async(req,res)=>{try{
if(!hosts.includes(req.headers.host))return json(res,403,{error:'Invalid host'});
if(req.headers.origin&&!origins.includes(req.headers.origin))return json(res,403,{error:'Invalid origin'});
if(basePath&&req.url===basePath){res.writeHead(302,{Location:basePath+'/'});return res.end()}
if(!stripBase(req)){res.writeHead(404);return res.end()}
if(await auth.handle(req,res))return;
const publicAssets={'/login':'login.html','/login.js':'login.js','/style.css':'style.css'};
if(publicAssets[req.url]&&req.method==='GET'){const file=publicAssets[req.url];res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html','Cache-Control':'no-store'});return res.end(fs.readFileSync(path.join(__dirname,'dist',file)))}
if(req.url==='/api/internal/follow-ups'){
 const remote=req.socket.remoteAddress||'';if(!['127.0.0.1','::1','::ffff:127.0.0.1'].includes(remote)||!safeToken(req.headers['x-bot4u-internal-token']))return json(res,403,{error:'Internal access denied.'});
 if(req.method!=='POST')return json(res,405,{error:'Method not allowed'});
 try{const data=await jsonBody(req);const owner=String(data.owner||'').toLowerCase();if(!auth.userExists(owner))return json(res,404,{error:'Account not found.'});requireAirtel(owner);return json(res,201,{followUp:followUps.create(owner,data,{source:'voice-agent'})})}catch(e){return json(res,e.status||400,{error:e.message})}
}
let identity=auth.session(req);
if(req.url.startsWith('/api/v1/')){
 const access=apiGateway(req);if(access.error){if(access.status===429)res.setHeader('Retry-After','60');return json(res,access.status,{error:access.error})}
 identity=access.identity;req.url=access.url;
}
if(!identity){if(req.url.startsWith('/api/'))return json(res,401,{error:'Please sign in.'});res.writeHead(302,{Location:basePath+'/login','Cache-Control':'no-store'});return res.end()}
if(req.url==='/api/numbers'){
 if(req.method!=='GET')return json(res,405,{error:'Method not allowed'});
 if(identity.username.toLowerCase()!=='huzaifa')return json(res,403,{error:'Airtel inventory is available to the account administrator only.'});
 try{const inventory=JSON.parse(fs.readFileSync(path.join(__dirname,'.local','airtel-inventory.json'),'utf8'));const mappings=JSON.parse(fs.readFileSync(path.join(__dirname,'.local','telephony.json'),'utf8'));const digits=v=>String(v||'').replace(/\D/g,'').slice(-10);const assigned=new Set(['8045911978']);const collect=v=>{if(typeof v==='string'&&/^\+?[\d ()-]{10,25}$/.test(v))assigned.add(digits(v));else if(v&&typeof v==='object')Object.values(v).forEach(collect)};collect(mappings);return json(res,200,{checkedAt:inventory.checkedAt,numbers:inventory.numbers.filter(n=>!assigned.has(digits(n)))});}catch{return json(res,503,{error:'Airtel inventory is not available. Ask the administrator to update it.'})}
}
if(req.url==='/api/keys'||req.url.startsWith('/api/keys/')){
 // Key lifecycle endpoints require the dashboard session, never a bearer key.
 if(!auth.session(req))return json(res,401,{error:'Please sign in.'});
 const owner=identity.username;
 if(!keyManagementLimit(owner))return json(res,429,{error:'Too many key management requests.'});
 if(req.url==='/api/keys'&&req.method==='GET')return json(res,200,{keys:apiKeys.list(owner)});
 if(req.url==='/api/keys'&&req.method==='POST'){
  if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
  let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>2048)return json(res,413,{error:'Request too large'})}
  let data;try{data=JSON.parse(body)}catch{return json(res,400,{error:'Invalid JSON'})}
  try{return json(res,201,apiKeys.create(owner,data?.name))}catch(e){return json(res,400,{error:e.message})}
 }
 if(req.method==='DELETE'&&/^\/api\/keys\/[a-f0-9]{32}$/.test(req.url))return apiKeys.revoke(owner,req.url.split('/').pop())?json(res,200,{revoked:true}):json(res,404,{error:'Key not found'});
 return json(res,405,{error:'Method not allowed'});
}
const requestUrl=new URL(req.url,'http://localhost');
if(requestUrl.pathname==='/api/follow-ups/import'){
 if(req.method!=='POST')return json(res,405,{error:'Method not allowed'});
 try{requireAirtel(identity.username);const data=await jsonBody(req,600000);let rows,source;if(typeof data.csv==='string'){rows=parseFollowUpCsv(data.csv);source='csv'}else if(Array.isArray(data.records)){if(!data.records.length||data.records.length>200)throw Error('Provide between 1 and 200 database records.');rows=data.records.map((record,index)=>({row:index+1,data:record,sourceRef:String(record.sourceRef||record.id||'').trim()}));source='database'}else throw Error('Provide a CSV string or a records array.');const result=followUps.importBatch(identity.username,rows,{source});return json(res,result.imported.length?201:200,result)}catch(e){return json(res,e.status||400,{error:e.message})}
}
if(requestUrl.pathname==='/api/follow-ups'||/^\/api\/follow-ups\/[a-f0-9-]{36}$/.test(requestUrl.pathname)){
 const owner=identity.username,id=requestUrl.pathname.split('/')[3];
 if(req.method==='GET'&&!id)return json(res,200,{followUps:followUps.list(owner,{filter:requestUrl.searchParams.get('filter')||'All',search:requestUrl.searchParams.get('search')||''})});
 if(req.method==='POST'&&!id){try{requireAirtel(owner);return json(res,201,{followUp:followUps.create(owner,await jsonBody(req))})}catch(e){return json(res,e.status||400,{error:e.message})}}
 if(req.method!=='PATCH'||!id)return json(res,405,{error:'Method not allowed'});
 try{const data=await jsonBody(req),action=data.action||'edit';if(action==='call_now'){requireAirtel(owner);const result=await followUpScheduler.callNow(owner,id);return json(res,result.status,result.body)}const record=action==='cancel'?followUps.cancel(owner,id):action==='reschedule'?followUps.reschedule(owner,id,data):action==='edit'?followUps.edit(owner,id,data):null;if(!record)return json(res,400,{error:'Invalid follow-up action.'});return json(res,200,{followUp:record})}catch(e){return json(res,/not found/i.test(e.message)?404:409,{error:e.message})}
}
if(requestUrl.pathname==='/api/call-recording'&&['GET','HEAD'].includes(req.method)){
const file=history.recording(identity.username,requestUrl.searchParams.get('id'));
if(!file)return json(res,404,{error:'Recording unavailable'});
const size=fs.statSync(file).size;let start=0,end=size-1,status=200;
const headers={'Content-Type':'audio/wav','Cache-Control':'private, no-store','Accept-Ranges':'bytes','X-Content-Type-Options':'nosniff'};
if(req.headers.range){const match=/^bytes=(\d*)-(\d*)$/.exec(req.headers.range);if(!match||(!match[1]&&!match[2])){res.writeHead(416,{'Content-Range':'bytes */'+size});return res.end()}
if(match[1]){start=Number(match[1]);end=match[2]?Math.min(Number(match[2]),end):end}else start=Math.max(0,size-Number(match[2]));
if(start>end||start>=size){res.writeHead(416,{'Content-Range':'bytes */'+size});return res.end()}status=206;headers['Content-Range']='bytes '+start+'-'+end+'/'+size;}
headers['Content-Length']=end-start+1;res.writeHead(status,headers);if(req.method==='HEAD')return res.end();const stream=fs.createReadStream(file,{start,end});stream.on('error',()=>res.destroy());res.on('close',()=>stream.destroy());return stream.pipe(res);
}
if(requestUrl.pathname==='/api/call-history'){

const owner=identity.username;
if(req.method==='GET')return json(res,200,{calls:history.list(owner)});
if(req.method!=='PATCH')return json(res,405,{error:'Method not allowed'});
if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
let text='';for await(const chunk of req){text+=chunk;if(Buffer.byteLength(text)>8000)return json(res,413,{error:'Request too large'})}
let data;try{data=JSON.parse(text)}catch{return json(res,400,{error:'Invalid JSON'})}
if(!data||typeof data.remarks!=='string'||data.remarks.length>2000||!['','Answered','Not answered'].includes(data.result))return json(res,400,{error:'Invalid remarks or result'});
return history.edit(owner,data.id,data)?json(res,200,{saved:true}):json(res,404,{error:'Call not found'});
}
if(requestUrl.pathname==='/api/phone/campaign'){
const owner=identity.username;
if(req.method==='GET'){await campaigns.tick(owner);return json(res,200,{campaign:campaigns.get(owner)})}
if(req.method!=='POST')return json(res,405,{error:'Method not allowed'});
if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>300000)return json(res,413,{error:'CSV is too large.'})}
try{const data=JSON.parse(body);const campaign=data.action==='import'?campaigns.importCsv(owner,data.csv):campaigns.action(owner,data.action);return json(res,200,{campaign})}catch(e){return json(res,400,{error:e.message})}
}
if(requestUrl.pathname==='/api/phone/call'){
if(campaigns.locked(identity.username))return json(res,409,{error:'A CSV calling list is active. Pause it and finish the current call before calling manually.'});

if(req.method!=='POST')return json(res,405,{error:'Method not allowed'});
if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>4096)return json(res,413,{error:'Request too large'})}
let data;try{data=JSON.parse(body);if(!data||typeof data!=='object')throw Error()}catch{return json(res,400,{error:'Invalid request'})}
const result=await phoneCall(identity.username,data);return json(res,result.status,result.body);
}
if(requestUrl.pathname==='/api/account/telephony'){
 const owner=identity.username;
 if(req.method==='GET')return json(res,200,{telephony:carriers.read(owner)});
 if(req.method!=='PUT')return json(res,405,{error:'Method not allowed'});
 if(campaigns.locked(owner))return json(res,409,{error:'Pause the CSV list and finish its current call before switching carriers.'});
 if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
 let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>1024)return json(res,413,{error:'Request too large'});}
 try{return json(res,200,{telephony:carriers.select(owner,JSON.parse(body).provider)})}catch(e){return json(res,400,{error:e.message})}
}
if(requestUrl.pathname==='/api/email-status')return json(res,200,{configured:!!(process.env.RESEND_API_KEY&&process.env.BOOKING_EMAIL_FROM)});
if(requestUrl.pathname==='/api/script'){
const mode=requestUrl.searchParams.get('mode')||'inbound';if(!Object.hasOwn(promptPaths,mode))return json(res,400,{error:'Invalid call mode'});const owner=identity.username;
if(req.method==='GET')return json(res,200,{script:scripts.read(owner,mode)});
if(req.method!=='PUT')return json(res,405,{error:'Method not allowed'});
if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>100000)return json(res,413,{error:'Script is too long.'})}
let data;try{data=JSON.parse(body)}catch{return json(res,400,{error:'Invalid JSON'})}
if(typeof data.script!=='string'||!data.script.trim()||data.script.length>30000)return json(res,400,{error:'Enter a script between 1 and 30,000 characters.'});
scripts.write(owner,mode,data.script);return json(res,200,{saved:true});
}
const asset=assets[req.url];if(req.method!=='GET'||!asset){res.writeHead(404);return res.end()};res.writeHead(200,{'Content-Type':asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'text/html','Cache-Control':'no-store'});res.end(fs.readFileSync(path.join(__dirname,'dist',asset)));
}catch{json(res,500,{error:'Could not read or save the script. Please retry.'})}});
const wss=new WebSocketServer({noServer:true,maxPayload:128*1024});
server.on('upgrade',(req,socket,head)=>{if(!stripBase(req)){socket.destroy();return}const session=auth.session(req);if(!session){socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');socket.destroy();return}const url=new URL(req.url,'http://localhost');const mode=url.searchParams.get('mode')||'inbound';const email=(url.searchParams.get('email')||'').trim();const name=(url.searchParams.get('name')||'').trim();if((email&&!validEmail(email))||!Object.hasOwn(promptPaths,mode)||!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(name)||url.pathname!=='/live'||!hosts.includes(req.headers.host)||!origins.includes(req.headers.origin)){socket.destroy();return}const selectedScript=scripts.read(session.username,mode);if(!selectedScript.trim()){socket.write('HTTP/1.1 409 Conflict\r\nConnection: close\r\n\r\n');socket.destroy();return}wss.handleUpgrade(req,socket,head,client=>{session.sockets.add(client);const expiry=setTimeout(()=>client.close(1000,'Session expired'),Math.max(0,session.expires-Date.now()));client.on('close',()=>{clearTimeout(expiry);session.sockets.delete(client)});wss.emit('connection',client,name,mode,email,selectedScript,session.username)})});
wss.on('connection',(client,name,mode,email,selectedScript,owner)=>{
const browserCall=require('./browser-history.cjs').trackBrowserCall(history,owner,{name,mode});
client.once('close',()=>browserCall.end());
const mailer=createBookingMailer();
const voiceDefaults=fs.readFileSync(path.join(__dirname,'voice-defaults.txt'),'utf8');
const sessionPrompt=selectedScript.replaceAll('{{customer_name}}',()=>name).replaceAll('{{customer_email}}',()=>email||'not provided')+'\n\n'+voiceDefaults+'\nCall control: After speaking your complete final goodbye, call end_conversation so the app disconnects after playback. Do not call it while awaiting a customer response.\nFollow-ups: If the customer asks for a callback or says to call later, do not ask for a date, time, timezone, name, or phone number. Simply acknowledge with a brief phrase such as "Okay, we will follow up," then call mark_follow_up_requested. Do not claim that a specific callback has been scheduled.';
let ready=false;
const upstream=new WebSocket('wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent',{headers:{'x-goog-api-key':key},handshakeTimeout:15000});
const send=data=>{if(client.readyState===WebSocket.OPEN)client.send(JSON.stringify(data))};
const timeout=setTimeout(()=>{send({error:'Connection timed out. Please reconnect.'});client.close();upstream.close()},20000);
upstream.on('open',()=>upstream.send(JSON.stringify({setup:{model:`models/${model.replace(/^models\//,'')}`,generationConfig:{responseModalities:['AUDIO'],thinkingConfig:{thinkingLevel:'minimal'},speechConfig:{voiceConfig:{prebuiltVoiceConfig:{voiceName:'Sulafat'}}}},systemInstruction:{parts:[{text:sessionPrompt}]},tools:[{functionDeclarations:[{name:"send_booking_link",description:"Email the discovery-call link only after the customer confirms the exact email address and agrees to receive it. Report only the returned sending status.",parameters:{type:"OBJECT",properties:{email:{type:"STRING"},confirmed:{type:"BOOLEAN"}},required:["email","confirmed"]}},{name:"mark_follow_up_requested",description:"Record the call remark as Follow up when the customer asks to be called back. Do not collect scheduling details."},{name:"end_conversation",description:"End the call after your spoken closing statement has been delivered. Call only after saying goodbye, never while asking a question or waiting for a reply."}]}],inputAudioTranscription:{},outputAudioTranscription:{},realtimeInputConfig:{automaticActivityDetection:{disabled:false,startOfSpeechSensitivity:'START_SENSITIVITY_LOW',endOfSpeechSensitivity:'END_SENSITIVITY_LOW',prefixPaddingMs:120,silenceDurationMs:650},activityHandling:'START_OF_ACTIVITY_INTERRUPTS'}}})));
upstream.on('message',async raw=>{let data;try{data=JSON.parse(raw)}catch{return}if(data.setupComplete){browserCall.connected();ready=true;clearTimeout(timeout);send({ready:true});upstream.send(JSON.stringify({realtimeInput:{text:'The customer has connected. Greet them now with your short opening question.'}}))}if(data.serverContent)send({serverContent:data.serverContent});for(const call of data.toolCall?.functionCalls||[]){let result;if(call.name==='send_booking_link'){result=await mailer(call.args||{});send({emailResult:result})}else if(call.name==='mark_follow_up_requested'){browserCall.markFollowUp();result={status:'recorded',remark:'Follow up'}}else if(call.name==='end_conversation'){ready=false;send({endAfterPlayback:true});result={status:'ending_after_playback'}}if(result&&upstream.readyState===WebSocket.OPEN)upstream.send(JSON.stringify({toolResponse:{functionResponses:[{id:call.id,name:call.name,response:result}]}}))}if(data.goAway)send({notice:'This session is ending soon. Reconnect to continue.'});if(data.error){send({error:'Gemini Live could not start. Please reconnect.'});client.close()}});
client.on('message',raw=>{if(!ready||upstream.readyState!==WebSocket.OPEN)return;try{const m=JSON.parse(raw);if(m.audio&&typeof m.audio==='string'&&m.audio.length<64000){upstream.send(JSON.stringify({realtimeInput:{audio:{data:m.audio,mimeType:'audio/pcm;rate=16000'}}}))}else if(typeof m.text==='string'&&m.text.length<=8000){upstream.send(JSON.stringify({realtimeInput:{text:m.text}}))}else if(m.audioStreamEnd){upstream.send(JSON.stringify({realtimeInput:{audioStreamEnd:true}}))}}catch{client.close(1003)}});
upstream.on('error',()=>{send({error:'Cannot connect to Gemini Live. Please reconnect.'});client.close()});
upstream.on('close',(code,reason)=>{clearTimeout(timeout);if(code!==1000)console.log('Gemini Live closed:',code,reason.toString().replace(/AIza[\w-]+/g,'[redacted]'));send({closed:true});client.close()});
client.on('close',()=>{clearTimeout(timeout);upstream.close()});client.on('error',()=>upstream.close());
});
server.listen(port,'127.0.0.1',()=>console.log(`Live voice bot: http://127.0.0.1:${port}`));
