const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');

test('dashboard sidebar includes the referenced account plans and prices',()=>{
 const html=fs.readFileSync('dist/dashboard.html','utf8'),script=fs.readFileSync('dist/dashboard.js','utf8');
 for(const value of ['Account Type','data-account-plan="entry">Entry','data-account-plan="growth">Growth','data-account-plan="scale">Scale','data-account-plan="enterprise">Enterprise'])assert.ok(html.includes(value),`missing ${value}`);
 for(const value of ['Pay-As-You-Go','₹1,500 setup','₹1,100/month','₹6/minute','Monthly Plan','₹15,000/month','3,000 minutes included','Yearly Plan','₹1,50,000/year','50,000 minutes included','Custom Plan','Unlimited minutes'])assert.ok(script.includes(value),`missing ${value}`);
 assert.match(html,/class="account-type-menu"/);
 assert.match(html,/id="accountPlanPanel"[^>]*hidden/);
 assert.match(script,/showAccountPlan/);
});

test('signup lets a user choose an account type and inspect its plan',()=>{
 const html=fs.readFileSync('dist/login.html','utf8'),script=fs.readFileSync('dist/login.js','utf8');
 for(const value of ['id="accountTypeField"','data-signup-plan="entry">Entry','data-signup-plan="growth">Growth','data-signup-plan="scale">Scale','data-signup-plan="enterprise">Enterprise','id="accountPlanPanel"'])assert.ok(html.includes(value),`missing ${value}`);
 for(const value of ['selectAccountPlan','accountType','₹15,000/month','₹1,50,000/year','Unlimited concurrency'])assert.ok(script.includes(value),`missing ${value}`);
});
