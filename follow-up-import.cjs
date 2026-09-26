const HEADER_ALIASES={
 customerName:['customername','name','fullname','leadname'],
 phoneNumber:['phonenumber','phone','number','mobile','leadphone'],
 date:['followupdate','scheduleddate','callbackdate','date'],
 time:['followuptime','scheduledtime','callbacktime','time'],
 timezone:['timezone','timezoneid'],
 reason:['followupreason','callbackreason','reason'],
 notes:['notes','note'],
 agentId:['agentid','agent','voiceagent'],
 maxAttempts:['maxattempts','attempts'],
 retryIntervalMinutes:['retryintervalminutes','retryminutes','retryinterval'],
 sourceRef:['sourceref','recordid','leadid','customerid','id']
};
const REQUIRED=['customerName','phoneNumber','date','time','reason'];

function key(value){return String(value||'').trim().toLowerCase().replace(/[\s_-]+/g,'')}
function parseRows(text){
 if(typeof text!=='string'||!text.trim())throw Error('Choose a non-empty CSV file.');
 if(Buffer.byteLength(text)>512*1024)throw Error('CSV file must be 512 KB or smaller.');
 const rows=[];let row=[],field='',quoted=false;
 for(let i=0;i<text.length;i++){
  const ch=text[i];
  if(quoted){if(ch==='"'&&text[i+1]==='"'){field+='"';i++}else if(ch==='"')quoted=false;else field+=ch;continue}
  if(ch==='"'&&!field)quoted=true;else if(ch===','){row.push(field.trim());field=''}else if(ch==='\n'){row.push(field.trim());rows.push(row);row=[];field=''}else if(ch!=='\r')field+=ch;
 }
 if(quoted)throw Error('CSV contains an unclosed quoted value.');
 row.push(field.trim());if(row.some(Boolean))rows.push(row);
 return rows.filter(values=>values.some(Boolean));
}
function parseFollowUpCsv(text){
 const rows=parseRows(text.replace(/^\uFEFF/,''));
 if(rows.length<2)throw Error('CSV must include a header and at least one data row.');
 if(rows.length>201)throw Error('CSV can contain at most 200 follow-ups.');
 const headers=rows[0].map(key),columns={};
 for(const [field,aliases] of Object.entries(HEADER_ALIASES)){
  const indexes=headers.map((header,index)=>aliases.includes(header)?index:-1).filter(index=>index>=0);
  if(indexes.length>1)throw Error(`CSV contains more than one column for ${field}.`);
  if(indexes.length)columns[field]=indexes[0];
 }
 const missing=REQUIRED.filter(field=>columns[field]==null);
 if(missing.length)throw Error(`CSV is missing required columns: ${missing.join(', ')}.`);
 return rows.slice(1).map((values,index)=>{
  const data={};for(const [field,column] of Object.entries(columns))data[field]=values[column]??'';
  if(!data.timezone)data.timezone='Asia/Kolkata';
  return {row:index+2,data,sourceRef:String(data.sourceRef||'').trim()};
 });
}

module.exports={parseFollowUpCsv};
