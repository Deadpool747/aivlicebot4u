const $=id=>document.getElementById(id);
let phoneCalling=false,carrierSaving=false;
async function placePhoneCall(){
 if(phoneCalling||carrierSaving)return;
 const status=$('phoneCallStatus');
 if(mode!=='outbound'||!scriptLoaded||savingScript||!savedScript.trim()||$('script').value!==savedScript){status.textContent='Select Outbound and save your script before calling.';return}
 const number=$('outboundNumber').value.trim();
 if(!/^\+[1-9]\d{6,14}$/.test(number.replace(/[ ()-]/g,''))){status.textContent='Enter the number with + and country code.';$('outboundNumber').focus();return}
 phoneCalling=true;$('phoneCall').disabled=true;$('phoneCall').textContent='Calling…';status.textContent='Submitting call request…';
 try{const r=await fetch('api/phone/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({number,name:$('customerName').value.trim()})});const d=await r.json();status.textContent=r.ok?d.message:(d.error||'Could not request the call.');}
 catch{status.textContent='Call status is unknown. Check your carrier call history before trying again.';}
 finally{phoneCalling=false;$('phoneCall').disabled=false;$('phoneCall').textContent='Call';}
}
$('phoneCall').onclick=placePhoneCall;
$('outboundNumber').addEventListener('input',()=>{if(!phoneCalling)$('phoneCallStatus').textContent='';});
let socket,context,stream,capture,source,silentGain,connected=false,muted=false,nextTime=0,attempt=0;
let ending=false;
function finishWhenDrained(){if(ending&&playing.size===0){error('');finish()}}
const playing=new Set();let userLine=null,agentLine=null;
const error=text=>{$('error').textContent=text;$('error').hidden=!text};
function stopAudio(){for(const node of playing){try{node.stop()}catch{}}playing.clear();nextTime=0}
function finish(){ending=false;attempt++;connected=false;updateScriptButton();socket?.close();socket=null;capture?.disconnect();source?.disconnect();silentGain?.disconnect();stream?.getTracks().forEach(t=>t.stop());stream=null;stopAudio();context?.close();context=null;capture=null;muted=false;$('mute').textContent='Mute microphone';$('mute').setAttribute('aria-pressed','false');$('nameField').hidden=mode==='inbound';$('connect').hidden=false;$('connect').disabled=mode==='outbound'&&!$('customerName').value.trim();$('customerName').disabled=false;$('outboundNumber').disabled=false;$('customerEmail').disabled=false;$('callMode').disabled=false;$('connect').textContent='Start conversation';$('session').hidden=true;$('status').textContent='Conversation ended.';userLine=null;agentLine=null}
function transcript(role,text){}
function play(data,rate=24000){if(!context||context.state!=='running')return;const bytes=Uint8Array.from(atob(data),c=>c.charCodeAt(0));const view=new DataView(bytes.buffer);const buffer=context.createBuffer(1,bytes.length/2,rate);const samples=buffer.getChannelData(0);for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(i*2,true)/32768;const node=context.createBufferSource();node.buffer=buffer;node.connect(context.destination);nextTime=nextTime<=context.currentTime?context.currentTime+0.12:nextTime;node.start(nextTime);nextTime+=buffer.duration;playing.add(node);$('activity').textContent='BOT4U is speaking · You can interrupt';node.onended=()=>{playing.delete(node);finishWhenDrained();if(!playing.size&&connected)$('activity').textContent=muted?'Microphone muted':'Listening…'}}
async function connect(){if(!scriptLoaded||savingScript){error('Wait for the script to load or save.');return}if(!savedScript.trim()){error('Add and save a script for this call mode first.');$('script').focus();return}if($('script').value!==savedScript){error('Save your script changes before starting the conversation.');$('saveScript').focus();return}const phone=$('outboundNumber').value.trim();if(mode==='outbound'&&phone&&!/^\+[1-9]\d{6,14}$/.test(phone.replace(/[ ()-]/g,''))){error('Enter a phone number with country code, for example +91 98765 43210.');$('outboundNumber').focus();return}const email=mode==='inbound'?'':$('customerEmail').value.trim();if(email&&!$('customerEmail').checkValidity()){error('Enter a valid email address.');$('customerEmail').focus();return}const name=mode==='inbound'?'Guest':$('customerName').value.trim();if(!/^[\p{L}\p{M} .’'-]{1,80}$/u.test(name)){error('Enter a client name using letters, spaces, apostrophes or hyphens.');$('customerName').focus();return}const id=++attempt;$('connect').disabled=true;$('customerName').disabled=true;$('outboundNumber').disabled=true;$('customerEmail').disabled=true;$('callMode').disabled=true;error('');$('status').textContent='Connecting…';try{
context=new AudioContext({latencyHint:'interactive'});await context.resume();if(id!==attempt)return;
stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});if(id!==attempt){stream.getTracks().forEach(t=>t.stop());return}
await context.audioWorklet.addModule('capture.js');if(id!==attempt)return;
capture=new AudioWorkletNode(context,'pcm-capture');source=context.createMediaStreamSource(stream);silentGain=context.createGain();silentGain.gain.value=0;source.connect(capture);capture.connect(silentGain);silentGain.connect(context.destination);
capture.port.onmessage=e=>{if(!connected||ending||muted||socket?.readyState!==WebSocket.OPEN)return;if(socket.bufferedAmount>128000){error('Connection is too slow. Reconnect to continue.');finish();return}const bytes=new Uint8Array(e.data);let raw='';for(const b of bytes)raw+=String.fromCharCode(b);socket.send(JSON.stringify({audio:btoa(raw)}))};
socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}${location.pathname.replace(/[^/]*$/, '')}live?name=${encodeURIComponent(name)}&mode=${mode}&email=${encodeURIComponent(email)}`);
socket.onmessage=e=>{if(id!==attempt)return;const m=JSON.parse(e.data);if(m.error){error(m.error);finish();return}if(m.emailResult){$('emailStatus').textContent=m.emailResult.message}if(m.ready){connected=true;updateScriptButton();$('nameField').hidden=true;$('connect').hidden=true;$('session').hidden=false;$('status').textContent='Connected · Speak naturally, anytime.';$('activity').textContent='BOT4U is joining…'}if(m.endAfterPlayback){ending=true;stream?.getTracks().forEach(t=>t.stop());$('status').textContent='Ending after BOT4U’s goodbye…';finishWhenDrained();return}if(m.notice)error(m.notice);const c=m.serverContent;if(!c)return;if(c.interrupted){stopAudio();agentLine=null;$('activity').textContent='Listening…'}if(c.inputTranscription)transcript('user',c.inputTranscription.text);if(c.outputTranscription)transcript('model',c.outputTranscription.text);for(const part of c.modelTurn?.parts||[]){if(part.inlineData?.data)play(part.inlineData.data,Number(part.inlineData.mimeType?.match(/rate=(\d+)/)?.[1]||24000))}if(c.turnComplete){userLine=null;agentLine=null}};
socket.onerror=()=>{if(id===attempt)error('The live connection failed. Please reconnect.')};socket.onclose=()=>{if(id===attempt){error('Live connection closed. Click Start conversation to continue.');finish()}};
}catch(err){if(id!==attempt)return;error(err.name==='NotAllowedError'?'Allow microphone access, then connect again.':err.name==='NotFoundError'?'No microphone found. Connect a microphone and try again.':'Could not start live audio. Please reconnect.');finish()}}
$('customerName').oninput=()=>{$('connect').disabled=mode==='outbound'&&!$('customerName').value.trim()};
$('customerName').onkeydown=e=>{if(e.key==='Enter'&&!$('connect').disabled){e.preventDefault();connect()}};
$('connect').onclick=connect;$('end').onclick=()=>{error('');finish()};
$('mute').onclick=()=>{muted=!muted;stream?.getAudioTracks().forEach(t=>t.enabled=!muted);if(muted){socket?.send(JSON.stringify({audioStreamEnd:true}))}$('mute').textContent=muted?'Unmute microphone':'Mute microphone';$('mute').setAttribute('aria-pressed',String(muted));$('activity').textContent=muted?'Microphone muted':'Listening…'};
window.addEventListener('pagehide',finish);

let mode='inbound',drafts={},savedScript='',scriptLoaded=false,savingScript=false,loadId=0;
function updateScriptButton(){ $('saveScript').textContent=connected?'Save script (restart required)':'Save script'; }
async function loadScript(){const id=++loadId;scriptLoaded=false;$('script').disabled=true;$('saveScript').disabled=true;$('scriptTitle').textContent=mode==='inbound'?'Inbound script':'Outbound script';try{const r=await fetch('api/script?mode='+mode);if(!r.ok)throw Error();const data=await r.json();if(id!==loadId)return;savedScript=data.script;$('script').value=drafts[mode]??data.script;scriptLoaded=true;$('script').disabled=false;$('saveScript').disabled=false;$('scriptStatus').textContent=$('script').value!==savedScript?'Unsaved changes for this mode.':savedScript.trim()?'Saved '+mode+' script loaded.':'Add your '+mode+' script, then save it to start.'}catch{if(id===loadId)$('scriptStatus').textContent='Could not load script. Switch mode or refresh to retry.'}}
for(const choice of ['inbound','outbound'])$(choice).onchange=()=>{if(connected||savingScript||$('customerName').disabled){$('inbound').checked=mode==='inbound';$('outbound').checked=mode==='outbound';return}if(scriptLoaded)drafts[mode]=$('script').value;mode=choice;updateContactFields();$('outboundNumberField').hidden=mode!=='outbound';$('inbound').checked=mode==='inbound';$('outbound').checked=mode==='outbound';loadScript()};
loadScript();
$('script').oninput=()=>{$('scriptStatus').textContent='Unsaved changes. Save to apply to your next conversation.'};
$('saveScript').onclick=async()=>{if(savingScript)return;const script=$('script').value;if(!script.trim()){ $('scriptStatus').textContent='The script cannot be empty.';return }savingScript=true;$('saveScript').disabled=true;try{const r=await fetch('api/script?mode='+mode,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({script})});const d=await r.json();if(!r.ok)throw Error(d.error||'Could not save.');savedScript=script;drafts[mode]=script;$('scriptStatus').textContent=connected?'Saved. End this conversation and start again to use the new script.':'Saved. Start a conversation to use this script.';}catch(e){$('scriptStatus').textContent=e.message}finally{savingScript=false;$('saveScript').disabled=false}};


fetch('api/email-status').then(r=>r.json()).then(d=>{if(!d.configured)$('emailStatus').textContent='Email sending needs a sender account configured. You can still use the booking button.'}).catch(()=>{$('emailStatus').textContent='Email service status unavailable.'});

$('logout').onclick=async()=>{finish();try{const r=await fetch('api/auth/logout',{method:'POST'});if(!r.ok)throw Error();location.replace('login')}catch{error('Could not sign out. Please try again.')}};

fetch('api/auth/status').then(r=>r.json()).then(d=>{if(!d.authenticated){location.replace('login');return}document.getElementById('sidebarUsername').textContent=(d.username||'').toUpperCase();}).catch(()=>{});

let selectedCarrier='';
function showCarrier(m){
 if(!m)return;
 $('accountPhone').hidden=false;selectedCarrier=m.outbound_provider||m.provider||'piopiy';
 document.querySelectorAll('input[name="carrier"]').forEach(c=>c.checked=c.value===selectedCarrier);
 $('phoneBusiness').textContent=m.business_name||'Your phone account';
 $('phoneNumber').textContent=selectedCarrier==='airtel_iq'?'Airtel number: +91 8045911978':'Piopiy number: '+m.display_number;
 $('phoneState').textContent=selectedCarrier==='airtel_iq'?'Inbound and outbound calls use Airtel.':'Inbound and outbound use Piopiy. Carrier-side inbound routing must be configured.';
}
fetch('api/account/telephony').then(r=>r.json()).then(d=>showCarrier(d.telephony)).catch(()=>{});
for(const input of document.querySelectorAll('input[name="carrier"]'))input.onchange=async()=>{
 const controls=[...document.querySelectorAll('input[name="carrier"]')];
 if(phoneCalling){controls.forEach(c=>c.checked=c.value===selectedCarrier);$('carrierStatus').textContent='Wait for the current call request before switching.';return;}
 carrierSaving=true;$('phoneCall').disabled=true;controls.forEach(c=>c.disabled=true);$('carrierStatus').textContent='Saving provider...';
 try{const r=await fetch('api/account/telephony',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:input.value})});const d=await r.json();if(!r.ok)throw Error(d.error);showCarrier(d.telephony);$('phoneCallStatus').textContent='';$('carrierStatus').textContent='Saved for new inbound and outbound calls. Existing calls continue.';}
 catch(e){controls.forEach(c=>c.checked=c.value===selectedCarrier);$('carrierStatus').textContent=e.message||'Could not save provider.';}
 finally{carrierSaving=false;$('phoneCall').disabled=phoneCalling;controls.forEach(c=>c.disabled=false);}
};

function updateContactFields(){const inbound=mode==='inbound';$('nameField').hidden=inbound;$('emailField').hidden=inbound;$('customerName').required=!inbound;$('connect').disabled=!inbound&&!$('customerName').value.trim();$('status').textContent=inbound?'Start your inbound conversation with BOT4U.':'Enter your name, then start your conversation with BOT4U.';}
updateContactFields();
