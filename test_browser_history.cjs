const {test}=require('node:test');
const assert=require('node:assert/strict');
const {trackBrowserCall}=require('./browser-history.cjs');

test('callback requests are saved as a Follow up remark without scheduling details',()=>{
 const events=[];let now=Date.parse('2026-09-26T08:00:00Z');
 const call=trackBrowserCall({add(owner,event){events.push({owner,...event});return 'request-1'}},'huzaifa',{name:'Rahul',mode:'outbound'},()=>now);
 call.connected();call.markFollowUp();now+=12000;call.end();
 assert.equal(events.at(-1).result,'Answered');
 assert.equal(events.at(-1).remarks,'Follow up');
 assert.equal(events.at(-1).duration,12);
});
