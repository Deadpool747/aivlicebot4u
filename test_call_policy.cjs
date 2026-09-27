const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');

test('phone agents distinguish vague callbacks, exact schedules, and voicemail',()=>{
 for(const file of ['airtel_bot4u.py','piopiy-worker.py']){const source=fs.readFileSync(file,'utf8');assert.match(source,/mark_follow_up_requested/);assert.match(source,/schedule_follow_up/);assert.match(source,/voicemail_detected/);assert.match(source,/Asia\/Kolkata/)}
 const dashboard=fs.readFileSync('dist/dashboard.js','utf8');assert.doesNotMatch(dashboard,/\['Pending','Pending'\]/);assert.doesNotMatch(dashboard,/\['Unconfirmed','Unconfirmed'\]/);
});
