const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {parseLeads,createCampaigns}=require('./csv-calls.cjs');
const {googleSheetSource,fetchGoogleSheetCsv}=require('./google-sheets.cjs');
test('CSV supports BOM, quoted names, CRLF and skips duplicate and invalid numbers',()=>{
 const leads=parseLeads('\uFEFFname,phone\r\n"Aarav",+91 98765 43210\r\nOther,+919876543210\r\nBad,123');
 assert.equal(leads[0].number,'+919876543210');assert.equal(leads[0].state,'Ready');assert.match(leads[1].error,/Duplicate/);assert.match(leads[2].error,/valid phone/);
 assert.throws(()=>parseLeads('name,phone\n"unfinished,+919876543210'),/quote/);
 assert.throws(()=>parseLeads('name,phone,number\na,+123456789,+123456789'),/one phone/);
});
test('Indian 10-digit CSV and Google Sheet lead numbers receive +91 automatically',()=>{
 const leads=parseLeads('name,phone\nAarav,9876543210\nAsha,09123456789\nRavi,919876543211');
 assert.deepEqual(leads.map(lead=>lead.number),['+919876543210','+919123456789','+919876543211']);
 assert.ok(leads.every(lead=>lead.state==='Ready'));
});
test('Google Sheet links are restricted, converted to CSV exports and reject private HTML',async()=>{
 assert.deepEqual(googleSheetSource('https://docs.google.com/spreadsheets/d/sheet_ID-1/edit#gid=42'),{url:'https://docs.google.com/spreadsheets/d/sheet_ID-1/edit#gid=42',exportUrl:'https://docs.google.com/spreadsheets/d/sheet_ID-1/export?format=csv&gid=42'});
 assert.throws(()=>googleSheetSource('https://example.com/sheet.csv'),/docs.google.com/);
 await assert.rejects(()=>fetchGoogleSheetCsv('https://docs.google.com/spreadsheets/d/test/edit',async()=>({ok:true,arrayBuffer:async()=>Buffer.from('<html>sign in</html>')})),/publicly readable/);
});
test('Google Sheet live refresh bypasses cached exports',async()=>{
 let requestedUrl,requestedOptions;
 await fetchGoogleSheetCsv('https://docs.google.com/spreadsheets/d/test/edit',async(url,options)=>{requestedUrl=String(url);requestedOptions=options;return {ok:true,arrayBuffer:async()=>Buffer.from('name,phone\nA,9876543210')}});
 assert.match(requestedUrl,/export\?format=csv&_=/);
 assert.equal(requestedOptions.cache,'no-store');
 assert.equal(requestedOptions.headers['Cache-Control'],'no-cache');
});
test('saving a Google Sheet starts calls and later syncs only new phone numbers',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-sheet-'));let csv='name,phone\nA,+123456789',calls=0,clock=1;
 const q=createCampaigns({directory,now:()=>clock,sheetPollMs:10,loadSheet:async url=>({url,exportUrl:url+'/csv',csv}),call:async()=>({status:202,body:{historyId:'h'+(++calls)}}),history:{list:()=>[]}});
 try{const first=await q.saveSheet('one','https://docs.google.com/spreadsheets/d/test/edit');assert.equal(first.running,true);await q.tick('one');assert.equal(calls,1);q.action('one','pause');csv='name,phone\nA,+123456789\nB,+123456780';clock=20;await q.refreshSheet('one');assert.equal(q.get('one').leads.length,2);assert.equal(q.get('one').leads[1].state,'Ready');assert.equal(q.get('one').running,false);q.action('one','start');assert.equal(q.get('one').running,true)}finally{q.close();fs.rmSync(directory,{recursive:true,force:true})}
});
test('outbound page places a saved Google Sheet source beside the CSV source',()=>{
 const html=fs.readFileSync('dist/index.html','utf8'),script=fs.readFileSync('dist/csv-calls.js','utf8');
 for(const value of ['class="lead-source-grid"','id="sheetUrl"','id="sheetSave"','Save link and start calling'])assert.ok(html.includes(value),`missing ${value}`);
 assert.match(script,/save-sheet/);assert.match(script,/sheetStatus/);
 assert.match(script,/about every 5 seconds/);
});
test('queue calls once, waits for completion, respects pause, and isolates accounts',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-csv-'));let calls=0,rows=[];
 const q=createCampaigns({directory,call:async()=>({status:202,body:{historyId:'h'+(++calls)}}),history:{list:()=>rows}});
 try{
  q.importCsv('one','name,phone\nA,+123456789\nB,+123456780');assert.equal(q.get('two'),null);await q.tick('one');assert.equal(calls,0);
  q.action('one','start');await Promise.all([q.tick('one'),q.tick('one')]);assert.equal(calls,1);await q.tick('one');assert.equal(calls,1);
  assert.throws(()=>q.importCsv('one','phone\n+123456789'),/Pause/);
  q.action('one','pause');rows=[{id:'h1',callId:'call1',duration:15,result:'Answered'}];await q.tick('one');assert.equal(calls,1);assert.equal(q.get('one').current,null);
  q.action('one','start');await q.tick('one');assert.equal(calls,2);
 }finally{q.close();fs.rmSync(directory,{recursive:true,force:true})}
});
test('unconfirmed calls pause without redial and server restarts do not auto-resume',async()=>{
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'bot4u-csv-'));let clock=1,calls=0;
 const options={directory,now:()=>clock,call:async()=>{calls++;return {status:202,body:{historyId:'h'}}},history:{list:()=>[]}};
 let q=createCampaigns(options);
 try{
  q.importCsv('one','phone\n+123456789\n+123456780');q.action('one','start');await q.tick('one');q.close();q=createCampaigns(options);
  assert.equal(q.get('one').running,false);clock=400000;await q.tick('one');assert.equal(q.get('one').leads[0].state,'Unconfirmed');assert.throws(()=>q.action('one','start'),/Confirm/);assert.equal(calls,1);
  q.action('one','confirm-ended');q.action('one','start');await q.tick('one');assert.equal(calls,2);
 }finally{q.close();fs.rmSync(directory,{recursive:true,force:true})}
});
