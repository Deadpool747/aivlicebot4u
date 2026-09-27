const MAX_SHEET_BYTES=256000;

function googleSheetSource(value){
 let url;try{url=new URL(String(value||'').trim())}catch{throw Error('Enter a valid Google Sheet URL.')}
 if(url.protocol!=='https:'||url.hostname!=='docs.google.com')throw Error('Use a Google Sheet link from docs.google.com.');
 const match=/^\/spreadsheets\/d\/([a-zA-Z0-9_-]+)(?:\/|$)/.exec(url.pathname);
 if(!match)throw Error('Use a standard Google Sheet sharing link.');
 const hashGid=/(?:^|[&#])gid=(\d+)/.exec(url.hash),gid=url.searchParams.get('gid')||hashGid?.[1]||'';
 if(gid&&!/^\d+$/.test(gid))throw Error('The Google Sheet tab ID is invalid.');
 return {url:`https://docs.google.com/spreadsheets/d/${match[1]}/edit${gid?`#gid=${gid}`:''}`,exportUrl:`https://docs.google.com/spreadsheets/d/${match[1]}/export?format=csv${gid?`&gid=${gid}`:''}`};
}

async function fetchGoogleSheetCsv(value,fetchImpl=fetch){
 const source=googleSheetSource(value),response=await fetchImpl(source.exportUrl,{redirect:'follow',signal:AbortSignal.timeout(15000),headers:{Accept:'text/csv,text/plain;q=0.9'}});
 if(!response.ok)throw Error(`Google Sheet download failed (${response.status}).`);
 const bytes=Buffer.from(await response.arrayBuffer());
 if(bytes.length>MAX_SHEET_BYTES)throw Error('The Google Sheet is larger than 256 KB. Keep up to 200 lead rows.');
 const csv=bytes.toString('utf8');
 if(/<html|<!doctype/i.test(csv.slice(0,500)))throw Error('The Google Sheet is not publicly readable. Set General access to Anyone with the link.');
 return {...source,csv};
}

module.exports={googleSheetSource,fetchGoogleSheetCsv,MAX_SHEET_BYTES};
