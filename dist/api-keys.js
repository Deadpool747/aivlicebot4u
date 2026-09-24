if(document.getElementById('keys')){
'use strict';
const $=id=>document.getElementById(id);
async function request(url,options={}){const r=await fetch(url,{...options,cache:'no-store',headers:{'Content-Type':'application/json',...options.headers}});if(r.status===401){location.href='login';throw Error('Please sign in.')}const data=await r.json();if(!r.ok)throw Error(data.error||'Request failed.');return data}
const date=value=>value?new Date(value).toLocaleString():'Never';
async function refresh(){try{const data=await request('api/keys');$('keys').replaceChildren();for(const key of data.keys){const row=document.createElement('tr');for(const value of [key.name,key.prefix+'…',date(key.createdAt),date(key.lastUsedAt),key.status]){const cell=document.createElement('td');cell.textContent=value;row.append(cell)}const action=document.createElement('td');if(key.status==='Active'){const button=document.createElement('button');button.textContent='Revoke';button.onclick=async()=>{if(!confirm('Revoke '+key.name+'? Applications using this key will immediately lose access.'))return;button.disabled=true;try{await request('api/keys/'+key.id,{method:'DELETE'});await refresh()}catch(e){$('status').textContent=e.message;button.disabled=false}};action.append(button)}row.append(action);$('keys').append(row)}$('status').textContent=data.keys.length?'Generate a new key to replace an existing one, then revoke the old key.':'No API keys yet.'}catch(e){$('status').textContent=e.message}}
$('create').onclick=()=>{$('createForm').reset();$('createError').textContent='';$('createDialog').showModal();$('keyName').focus()};
$('cancelCreate').onclick=()=>$('createDialog').close();
$('createForm').onsubmit=async event=>{event.preventDefault();$('submitCreate').disabled=true;try{const data=await request('api/keys',{method:'POST',body:JSON.stringify({name:$('keyName').value})});$('createDialog').close();$('secret').value=data.token;$('copyStatus').textContent='';$('secretDialog').showModal();await refresh()}catch(e){$('createError').textContent=e.message}finally{$('submitCreate').disabled=false}};
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('secret').value);$('copyStatus').textContent='Copied.'}catch{$('secret').select();$('copyStatus').textContent='Copy the selected key manually.'}};
$('closeSecret').onclick=()=>$('secretDialog').close();$('secretDialog').addEventListener('close',()=>{$('secret').value='';$('copyStatus').textContent=''});window.addEventListener('pagehide',()=>{$('secret').value=''});refresh();

}

const sidebarLogout=document.getElementById('logout');
if(sidebarLogout)sidebarLogout.onclick=async()=>{sidebarLogout.disabled=true;try{const r=await fetch('api/auth/logout',{method:'POST'});if(!r.ok)throw Error();location.replace('login')}catch{document.getElementById('logoutStatus').textContent='Could not sign out. Please try again.';sidebarLogout.disabled=false}};

fetch('api/auth/status',{cache:'no-store'}).then(r=>r.json()).then(d=>{const el=document.getElementById('sidebarUsername');if(el&&d.authenticated)el.textContent=(d.username||'').toUpperCase();}).catch(()=>{});
