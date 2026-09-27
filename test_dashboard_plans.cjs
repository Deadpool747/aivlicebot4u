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
