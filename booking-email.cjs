const {randomUUID}=require('node:crypto');
const validEmail=email=>typeof email==='string'&&email.length<=254&&/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$/.test(email);
function createBookingMailer({apiKey=process.env.RESEND_API_KEY,from=process.env.BOOKING_EMAIL_FROM,fetcher=fetch}={}){
 const id=randomUUID();let job=null,recipient=null;
 return async({email,confirmed})=>{
  if(confirmed!==true)return {status:'not_sent',message:'Ask the customer to confirm the full email address and consent to receiving the link first.'};
  if(!validEmail(email))return {status:'not_sent',message:'The email address is invalid. Ask the customer to spell it and confirm again.'};
  if(!apiKey||!from)return {status:'not_configured',message:'Email sending is not configured. No email was sent. Offer the booking button instead.'};
  if(job)return recipient===email?job:{status:'not_sent',message:'A booking email has already been requested in this call. Do not send a duplicate.'};
  recipient=email;
  job=(async()=>{try{const r=await fetcher('https://api.resend.com/emails',{method:'POST',headers:{Authorization:`Bearer ${apiKey}`,'Content-Type':'application/json','Idempotency-Key':id},body:JSON.stringify({from,to:[email],subject:'Your Oswell Technologies discovery call link',text:'Thank you for your interest in Oswell Technologies.\n\nChoose a convenient time for your 20-minute discovery call:\nhttps://cal.com/fareez-khan/discovery-call\n\nYour meeting is not booked until you select and confirm a slot.\n\nOswell Technologies'}),signal:AbortSignal.timeout(15000)});const data=await r.json();return r.ok&&data.id?{status:'accepted',message:'The email service accepted the booking link email for sending. Inbox delivery is not yet verified.'}:{status:'failed',message:'The email service could not send the link. Offer the booking button instead.'}}catch{return {status:'unknown',message:'Email delivery could not be confirmed. Do not claim success or send again; offer the booking button.'}}})();return job;
 };
}
module.exports={createBookingMailer,validEmail};
