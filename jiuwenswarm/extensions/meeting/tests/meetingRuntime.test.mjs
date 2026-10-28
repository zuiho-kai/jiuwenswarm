import assert from 'node:assert/strict';
import test from 'node:test';
import { MeetingSession } from '../../../channels/web/frontend/node_modules/.cache/meeting/meetingRuntime.mjs';

const context = {leader:'leader',members:[
  {member_name:'leader',display_name:'Leader'},
  {member_name:'dev',display_name:'小王'},
  {member_name:'ann',display_name:'Ann'},
]};
const tick = () => new Promise(resolve=>setImmediate(resolve));

function harness({startCapture = async()=>{}} = {}) {
  const handlers = new Map();
  const requests = [], lines=[], errors=[], drafts=[], states=[];
  const resolvers = {};
  let onState;
  const transport = {
    on:(name,fn)=>{handlers.set(name,fn); return ()=>handlers.delete(name);},
    onStateChange:fn=>{onState=fn;return ()=>{onState=null;};},
    request:async(name,params)=>{
      requests.push({name,params});
      if(name === 'meeting.speech.synthesize') return new Promise(resolve=>{resolvers[name]=resolve;});
      return name === 'meeting.playback.ready' ? {ready:true,stream_id:'input'} : {accepted:true};
    },
  };
  const callbacks={line:line=>lines.push(line),error:code=>errors.push(code),draft:text=>drafts.push(text),recording:state=>states.push(state),ended:()=>{}};
  let onPcm, captureError;
  const capture={stopped:false,start:startCapture,stop(){this.stopped=true;}};
  const meeting=new MeetingSession(transport,'s',context,callbacks,(_stream,pcm,error)=>{onPcm=pcm;captureError=error;return capture;});
  const track={stop(){this.stopped=true;},onended:null};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[track],getAudioTracks:()=>[track]})}}});
  return {meeting,requests,handlers,track,lines,errors,drafts,states,resolvers,capture,callbacks,
    pcm:(samples=new Int16Array(1600).fill(1000))=>onPcm(samples), captureError:()=>captureError(),
    disconnect:()=>onState?.('closed')};
}

const speech = (extra={}) => ({session_id:'s', meeting_id:'meeting', request_id:'request', lease_token:'lease',
  source_message_id:'message', speaker_id:'dev', playback_id:'playback', voice:'Serena', chunks:['Summary'], chunk_count:1, ...extra});
const emitSpeech = (h,extra) => h.handlers.get('meeting.speech.ready')({payload:speech(extra)});
async function ready(h) { await h.meeting.start(); await h.meeting.activatePlayback('meeting','startup'); }
function audioHarness() {
  const audios=[];
  globalThis.Audio=class { constructor(){audios.push(this);} async play(){} pause(){this.paused=true;} };
  return audios;
}
test('ending discards late synthesis and keeps shutdown acknowledgements alive',async()=>{
  const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
  const request=h.requests.find(r=>r.name==='meeting.speech.synthesize');
  assert.equal(request.params.voice,undefined);assert.equal(request.params.text,undefined);
  h.meeting.stop();h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
  await tick();assert.equal(audios.length,0);assert.ok(h.track.stopped);
  assert.ok(h.handlers.has('meeting.playback.stop'));
  h.handlers.get('meeting.playback.stop')({payload:{session_id:'s',meeting_id:'meeting',stop_id:'stop'}});
  await tick();assert.ok(h.requests.some(r=>r.name==='meeting.playback.stopped'));
  assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'&&r.params.outcome==='completed'));
  h.meeting.finishCleanup();assert.equal(h.handlers.size,0);
});

test('only natural completion of every chunk reports completed',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h,{chunks:['First','Second'],chunk_count:2});await tick();
 const finishChunk=async(index)=>{h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:index});await tick();};
 await finishChunk(0);assert.equal(audios.length,1);
 assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'));
 audios[0].onended();await tick();await finishChunk(1);
 assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'));
 audios[1].onended();await tick();
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.completed_chunks,2);
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.outcome,'completed');
 h.meeting.stop();h.meeting.finishCleanup();
});

const inputEvent=(h,event,payload={})=>h.handlers.get(event)?.({payload:{session_id:'s',meeting_id:'meeting',stream_id:'input',utterance_id:1,...payload}});

test('microphone streams PCM before utterance completion and stays live across transcripts',async()=>{
 const h=harness();await ready(h);
 h.pcm();await tick();
 const chunk=h.requests.find(r=>r.name==='meeting.input.append');
 assert.equal(chunk.params.sequence,0);assert.equal(Buffer.from(chunk.params.audio_base64,'base64').length,3200);
 assert.ok(!h.requests.some(r=>r.name==='video.transcribe'));
 inputEvent(h,'meeting.input.transcribing');h.pcm();await tick();
 inputEvent(h,'meeting.input.transcript',{transcript:'First'});
 h.pcm();await tick();inputEvent(h,'meeting.input.transcript',{utterance_id:2,transcript:'Second'});
 assert.deepEqual(h.drafts,['First','Second']);assert.equal(h.states.at(-1),'recording');
 assert.deepEqual(h.requests.filter(r=>r.name==='meeting.input.append').map(r=>r.params.sequence),[0,1,2]);
 h.meeting.stop();h.meeting.finishCleanup();
});

const interruptSpeech=(h,extra={})=>h.handlers.get('meeting.speech.stop')({payload:{...speech(extra),stop_id:'interruption'}});

test('interruption stops matching audio, retains microphone, and plays the next speaker',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
 await tick();assert.equal(audios.length,1);
 const oldEnd=audios[0].onended;
 interruptSpeech(h);await tick();
 assert.ok(audios[0].paused);assert.ok(!h.capture.stopped);assert.ok(!h.track.stopped);
 assert.ok(h.requests.some(r=>r.name==='meeting.playback.stopped'&&r.params.stop_id==='interruption'));
 assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'));
 h.pcm();await tick();assert.ok(h.requests.some(r=>r.name==='meeting.input.append'));
 emitSpeech(h,{playback_id:'next',request_id:'next-request',lease_token:'next-lease'});await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'next',chunk_index:0});
 await tick();assert.equal(audios.length,2);
 interruptSpeech(h);await tick();assert.ok(!audios[1].paused); // Duplicate old stop cannot stop the new speaker.
 oldEnd();audios[1].onended();await tick();
 const results=h.requests.filter(r=>r.name==='meeting.playback.result');
 assert.equal(results.length,1);assert.equal(results[0].params.playback_id,'next');
 assert.equal(results[0].params.outcome,'completed');
 h.meeting.stop();h.meeting.finishCleanup();
});

test('interrupted synthesis cannot start late audio or replay a duplicate ready event',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 interruptSpeech(h);await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
 await tick();emitSpeech(h);await tick();assert.equal(audios.length,0);
 assert.equal(h.requests.filter(r=>r.name==='meeting.speech.synthesize').length,1);
 assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'));
 h.meeting.stop();h.meeting.finishCleanup();
});

test('human interruption is released only after public input acceptance',async()=>{
 const h=harness();await ready(h);
 let accept;h.callbacks.draft=()=>new Promise(resolve=>{accept=resolve;});
 inputEvent(h,'meeting.input.transcript',{transcript:'Wait, change that',interruption_id:'barrier'});
 await tick();assert.ok(!h.requests.some(r=>r.name==='meeting.input.admitted'));
 accept();await tick();
 const admitted=h.requests.find(r=>r.name==='meeting.input.admitted');
 assert.equal(admitted.params.interruption_id,'barrier');assert.equal(admitted.params.utterance_id,1);
 h.meeting.stop();h.meeting.finishCleanup();
});

test('failed human input and ended meetings never acknowledge admission',async()=>{
 const h=harness();await ready(h);
 h.callbacks.draft=async()=>{throw new Error('rejected');};
 inputEvent(h,'meeting.input.transcript',{transcript:'Wait',interruption_id:'barrier'});await tick();
 assert.ok(h.errors.includes('deliveryFailed'));assert.ok(!h.requests.some(r=>r.name==='meeting.input.admitted'));
 let accept;h.callbacks.draft=()=>new Promise(resolve=>{accept=resolve;});
 inputEvent(h,'meeting.input.transcript',{utterance_id:2,transcript:'Another',interruption_id:'barrier'});
 h.meeting.stop();accept();await tick();
 assert.ok(!h.requests.some(r=>r.name==='meeting.input.admitted'));
 h.meeting.finishCleanup();
});
