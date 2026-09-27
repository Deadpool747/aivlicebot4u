const labels={entry:'Entry',growth:'Growth',scale:'Scale',enterprise:'Enterprise'};
fetch('api/auth/status',{cache:'no-store'}).then(response=>response.json()).then(data=>{if(!data.authenticated){location.replace('login');return}document.getElementById('paymentPlan').textContent=labels[data.accountType]||'Entry'}).catch(()=>{document.getElementById('paymentPlan').textContent='Entry'});
