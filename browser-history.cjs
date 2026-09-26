function trackBrowserCall(history,owner,{name,mode},now=Date.now){
 const startedAt=new Date(now()).toISOString();
 const id=history.add(owner,{kind:'request',source:'browser',type:mode,phone:'',name,result:'Pending',remarks:'Website demo connecting.'});
 let connectedAt=null,finished=false,followUpRequested=false;
 function record(result,remarks,duration){history.add(owner,{kind:'session',source:'browser',requestId:id,callId:id,startedAt,type:mode,phone:'',name,result,remarks,duration})}
 return {
  connected(){if(finished||connectedAt!==null)return;connectedAt=now();record('Answered','Website demo in progress.',null)},
  markFollowUp(){if(!finished)followUpRequested=true},
  end(){if(finished)return;finished=true;record(connectedAt===null?'Not answered':'Answered',connectedAt===null?'Website demo could not connect.':followUpRequested?'Follow up':'Website demo ended.',connectedAt===null?null:Math.max(0,Math.round((now()-connectedAt)/1000)))}
 };
}
module.exports={trackBrowserCall};
