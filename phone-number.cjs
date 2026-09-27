function normalizeDialNumber(value){
 const number=String(value||'').trim().replace(/[ ()-]/g,'');
 if(/^[6-9]\d{9}$/.test(number))return '+91'+number;
 if(/^0[6-9]\d{9}$/.test(number))return '+91'+number.slice(1);
 if(/^91[6-9]\d{9}$/.test(number))return '+'+number;
 return number;
}

module.exports={normalizeDialNumber};
