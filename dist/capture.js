class PCMCapture extends AudioWorkletProcessor {
 constructor(){super();this.samples=[];this.position=0;this.chunk=new Int16Array(640);this.index=0}
 process(inputs){const input=inputs[0]?.[0];if(!input)return true;this.samples.push(...input);const step=sampleRate/16000;while(this.position+step<=this.samples.length){let sum=0,count=0;for(let i=Math.floor(this.position);i<Math.floor(this.position+step);i++){sum+=this.samples[i];count++}const sample=Math.max(-1,Math.min(1,sum/Math.max(1,count)));this.chunk[this.index++]=sample<0?sample*32768:sample*32767;this.position+=step;if(this.index===640){this.port.postMessage(this.chunk.buffer,[this.chunk.buffer]);this.chunk=new Int16Array(640);this.index=0}}const consumed=Math.floor(this.position);this.samples.splice(0,consumed);this.position-=consumed;return true}
}
registerProcessor('pcm-capture',PCMCapture);
