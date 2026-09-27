const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');

test('account plans are not shown in the logged-in dashboard',()=>{
 const html=fs.readFileSync('dist/dashboard.html','utf8'),script=fs.readFileSync('dist/dashboard.js','utf8');
 assert.doesNotMatch(html,/account-type-menu|data-account-plan|accountPlanPanel/);
 assert.doesNotMatch(script,/accountPlans|showAccountPlan|data-account-plan/);
});

test('signup lets a user choose an account type and inspect its plan',()=>{
 const html=fs.readFileSync('dist/login.html','utf8'),script=fs.readFileSync('dist/login.js','utf8');
 for(const value of ['id="accountTypeField"','data-signup-plan="entry">Entry','data-signup-plan="growth">Growth','data-signup-plan="scale">Scale','data-signup-plan="enterprise">Enterprise','id="accountPlanPanel"'])assert.ok(html.includes(value),`missing ${value}`);
 for(const value of ['selectAccountPlan','accountType','₹15,000/month','₹1,50,000/year','Unlimited concurrency'])assert.ok(script.includes(value),`missing ${value}`);
});

test('successful signup redirects to a protected dummy barcode page',()=>{
 const login=fs.readFileSync('dist/login.js','utf8'),html=fs.readFileSync('dist/payment-placeholder.html','utf8'),script=fs.readFileSync('dist/payment-placeholder.js','utf8'),server=fs.readFileSync('server.cjs','utf8');
 assert.match(login,/signup\?'payment-placeholder':'\.\/'/);
 assert.match(html,/BOT4U-DEMO-NO-PAYMENT/);
 assert.match(html,/No payment will be charged or collected/);
 assert.match(html,/Continue to BOT4U/);
 assert.match(script,/accountType/);
 assert.match(server,/payment-placeholder\.html/);
});

test('call dashboard displays purchased plan, remaining minutes and expiry',()=>{
 const html=fs.readFileSync('dist/dashboard.html','utf8'),script=fs.readFileSync('dist/dashboard.js','utf8'),server=fs.readFileSync('server.cjs','utf8');
 for(const value of ['Purchased plan','Minutes available','Plan expires','id="planName"','id="planMinutes"','id="planExpiry"'])assert.ok(html.includes(value),`missing ${value}`);
 assert.match(script,/api\/account\/plan/);
 assert.match(script,/renderPlan/);
 assert.match(server,/includedMinutes:3000/);
 assert.match(server,/includedMinutes:50000/);
});
