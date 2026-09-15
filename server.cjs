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
const port=Number(process.env.PORT||4174);
const hosts=[`127.0.0.1:${port}`,`localhost:${port}`];
const assets={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/capture.js':'capture.js'};
function json(res,status,data){res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data))}
const server=http.createServer(async(req,res)=>{try{
if(!hosts.includes(req.headers.host))return json(res,403,{error:'Invalid host'});
if(req.headers.origin&&!hosts.some(h=>req.headers.origin==='http://'+h))return json(res,403,{error:'Invalid origin'});
const requestUrl=new URL(req.url,'http://localhost');
if(requestUrl.pathname==='/api/email-status')return json(res,200,{configured:!!(process.env.RESEND_API_KEY&&process.env.BOOKING_EMAIL_FROM)});
if(requestUrl.pathname==='/api/script'){
const mode=requestUrl.searchParams.get('mode')||'inbound';if(!Object.hasOwn(promptPaths,mode))return json(res,400,{error:'Invalid call mode'});const promptPath=promptPaths[mode];
if(req.method==='GET')return json(res,200,{script:fs.readFileSync(promptPath,'utf8')});
if(req.method!=='PUT')return json(res,405,{error:'Method not allowed'});
if(!req.headers['content-type']?.startsWith('application/json'))return json(res,415,{error:'JSON required'});
let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>100000)return json(res,413,{error:'Script is too long.'})}
let data;try{data=JSON.parse(body)}catch{return json(res,400,{error:'Invalid JSON'})}
if(typeof data.script!=='string'||!data.script.trim()||data.script.length>30000)return json(res,400,{error:'Enter a script between 1 and 30,000 characters.'});
fs.writeFileSync(promptPath+'.tmp',data.script,'utf8');fs.renameSync(promptPath+'.tmp',promptPath);return json(res,200,{saved:true});
}
const asset=assets[req.url];if(req.method!=='GET'||!asset){res.writeHead(404);return res.end()};res.writeHead(200,{'Content-Type':asset.endsWith('.js')?'text/javascript':asset.endsWith('.css')?'text/css':'text/html','Cache-Control':'no-store'});res.end(fs.readFileSync(path.join(__dirname,'dist',asset)));
}catch{json(res,500,{error:'Could not read or save the script. Please retry.'})}});
const wss=new WebSocketServer({noServer:true,maxPayload:128*1024});
server.on('upgrade',(req,socket,head)=>{const url=new URL(req.url,'http://localhost');const mode=url.searchParams.get('mode')||'inbound';const email=(url.searchParams.get('email')||'').trim();const name=(url.searchParams.get('name')||'').trim();if((email&&!validEmail(email))||!Object.hasOwn(promptPaths,mode)||!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(name)||url.pathname!=='/live'||!hosts.includes(req.headers.host)||!hosts.some(h=>req.headers.origin===`http://${h}`)){socket.destroy();return}wss.handleUpgrade(req,socket,head,client=>wss.emit('connection',client,name,mode,email))});
wss.on('connection',(client,name,mode,email)=>{
const mailer=createBookingMailer();
const sessionPrompt=fs.readFileSync(promptPaths[mode],'utf8').replaceAll('{{customer_name}}',()=>name).replaceAll('{{customer_email}}',()=>email||'not provided')+'\nCall control: After speaking your complete final goodbye, call end_conversation so the app disconnects after playback. Do not call it while awaiting a customer response.';
let ready=false;
const upstream=new WebSocket('wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent',{headers:{'x-goog-api-key':key},handshakeTimeout:15000});
const send=data=>{if(client.readyState===WebSocket.OPEN)client.send(JSON.stringify(data))};
const timeout=setTimeout(()=>{send({error:'Connection timed out. Please reconnect.'});client.close();upstream.close()},20000);
upstream.on('open',()=>upstream.send(JSON.stringify({setup:{model:`models/${model.replace(/^models\//,'')}`,generationConfig:{responseModalities:['AUDIO'],thinkingConfig:{thinkingLevel:'minimal'},speechConfig:{voiceConfig:{prebuiltVoiceConfig:{voiceName:'Sulafat'}}}},systemInstruction:{parts:[{text:sessionPrompt}]},tools:[{functionDeclarations:[{name:"send_booking_link",description:"Email the discovery-call link only after the customer confirms the exact email address and agrees to receive it. Report only the returned sending status.",parameters:{type:"OBJECT",properties:{email:{type:"STRING"},confirmed:{type:"BOOLEAN"}},required:["email","confirmed"]}},{name:"end_conversation",description:"End the call after your spoken closing statement has been delivered. Call only after saying goodbye, never while asking a question or waiting for a reply."}]}],inputAudioTranscription:{},outputAudioTranscription:{},realtimeInputConfig:{automaticActivityDetection:{disabled:false,startOfSpeechSensitivity:'START_SENSITIVITY_HIGH',endOfSpeechSensitivity:'END_SENSITIVITY_HIGH',prefixPaddingMs:20,silenceDurationMs:400},activityHandling:'START_OF_ACTIVITY_INTERRUPTS'}}})));
upstream.on('message',async raw=>{let data;try{data=JSON.parse(raw)}catch{return}if(data.setupComplete){ready=true;clearTimeout(timeout);send({ready:true});upstream.send(JSON.stringify({realtimeInput:{text:'The customer has connected. Greet them now with your short opening question.'}}))}if(data.serverContent)send({serverContent:data.serverContent});for(const call of data.toolCall?.functionCalls||[]){if(call.name==='send_booking_link'){const result=await mailer(call.args||{});send({emailResult:result});if(upstream.readyState===WebSocket.OPEN)upstream.send(JSON.stringify({toolResponse:{functionResponses:[{id:call.id,name:call.name,response:result}]}}))}}if(data.toolCall?.functionCalls?.some(call=>call.name==='end_conversation')){ready=false;send({endAfterPlayback:true})}if(data.goAway)send({notice:'This session is ending soon. Reconnect to continue.'});if(data.error){send({error:'Gemini Live could not start. Please reconnect.'});client.close()}});
client.on('message',raw=>{if(!ready||upstream.readyState!==WebSocket.OPEN)return;try{const m=JSON.parse(raw);if(m.audio&&typeof m.audio==='string'&&m.audio.length<64000){upstream.send(JSON.stringify({realtimeInput:{audio:{data:m.audio,mimeType:'audio/pcm;rate=16000'}}}))}else if(typeof m.text==='string'&&m.text.length<=8000){upstream.send(JSON.stringify({realtimeInput:{text:m.text}}))}else if(m.audioStreamEnd){upstream.send(JSON.stringify({realtimeInput:{audioStreamEnd:true}}))}}catch{client.close(1003)}});
upstream.on('error',()=>{send({error:'Cannot connect to Gemini Live. Please reconnect.'});client.close()});
upstream.on('close',(code,reason)=>{clearTimeout(timeout);if(code!==1000)console.log('Gemini Live closed:',code,reason.toString().replace(/AIza[\w-]+/g,'[redacted]'));send({closed:true});client.close()});
client.on('close',()=>{clearTimeout(timeout);upstream.close()});client.on('error',()=>upstream.close());
});
server.listen(port,'127.0.0.1',()=>console.log(`Live voice bot: http://127.0.0.1:${port}`));
