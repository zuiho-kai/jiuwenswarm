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
  const resolvers = {}, rejectors = {};
  let onState;
  const transport = {
    on:(name,fn)=>{handlers.set(name,fn); return ()=>handlers.delete(name);},
    onStateChange:fn=>{onState=fn;return ()=>{onState=null;};},
    request:async(name,params)=>{
      requests.push({name,params});
      if(name === 'meeting.speech.synthesize') return new Promise((resolve,reject)=>{resolvers[name]=resolve;rejectors[name]=reject;});
      return name === 'meeting.playback.ready' ? {ready:true,stream_id:'input'} : {accepted:true};
    },
  };
  const callbacks={line:line=>lines.push(line),error:code=>errors.push(code),draft:text=>drafts.push(text),recording:state=>states.push(state),ended:()=>{}};
  let onPcm, captureError, onSpeech, onTurn;
  const capture={stopped:false,start:startCapture,stop(){this.stopped=true;}};
  const meeting=new MeetingSession(transport,'s',context,callbacks,(_stream,pcm,error,speech,turn)=>{onTurn=turn;onPcm=pcm;captureError=error;onSpeech=speech;return capture;});
  const track={stop(){this.stopped=true;},onended:null};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[track],getAudioTracks:()=>[track]})}}});
  return {meeting,requests,handlers,track,lines,errors,drafts,states,resolvers,rejectors,capture,callbacks,
    turn:(audio="data:audio/wav;base64,UklGRg==")=>onTurn(audio),
    speechDetected:(active=true)=>onSpeech(active), pcm:(samples=new Int16Array(1600).fill(1000))=>onPcm(samples), captureError:()=>captureError(),
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

for (const failure of ['synthesis', 'audio']) {
 test(`${failure} error is shown and the next queued speech still plays`,async()=>{
  const h=harness(),audios=audioHarness(),details=[];await ready(h);
  h.callbacks.error=(code,detail)=>{h.errors.push(code);details.push(detail);};
  emitSpeech(h);await tick();
  if(failure==='synthesis') {
   h.rejectors['meeting.speech.synthesize']({message:'Qwen did not read the supplied text faithfully'});
  } else {
   h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
   await tick();audios[0].onerror();
  }
  await tick();
  assert.deepEqual(h.errors,['playbackSkipped']);assert.ok(details[0]);
  const failed=h.requests.find(r=>r.name==='meeting.playback.result');
  assert.equal(failed.params.outcome,'failed');assert.equal(failed.params.completed_chunks,0);
  emitSpeech(h,{playback_id:'next',request_id:'next-request',lease_token:'next-lease'});await tick();
  h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'next',chunk_index:0});
  await tick();audios.at(-1).onended();await tick();
  const results=h.requests.filter(r=>r.name==='meeting.playback.result');
  assert.equal(results.length,2);assert.equal(results[1].params.playback_id,'next');
  assert.equal(results[1].params.outcome,'completed');assert.ok(!h.capture.stopped);
  h.meeting.stop();h.meeting.finishCleanup();
 });
}

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


test('pausing microphone leaves speech completion and the next speaker active', async () => {
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
 await tick();h.meeting.stopRecording();await tick();
 assert.ok(h.requests.some(r=>r.name==='meeting.input.pause'));
 assert.ok(!audios[0].paused);assert.ok(!h.capture.stopped);
 inputEvent(h,'meeting.input.transcript',{transcript:'Last words before mute'});await tick();
 assert.deepEqual(h.drafts,['Last words before mute']);
 audios[0].onended();await tick();
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.outcome,'completed');
 emitSpeech(h,{playback_id:'next',request_id:'next-request',lease_token:'next-lease'});await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'next',chunk_index:0});
 await tick();assert.equal(audios.length,2);audios[1].onended();await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.playback.result'&&r.params.outcome==='completed').length,2);
 h.meeting.stop();h.meeting.finishCleanup();
});


test('skip during synthesis advances without pausing the meeting or playing late audio', async () => {
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 await h.meeting.skipSpeech();await tick();
 const skipped=h.requests.find(r=>r.name==='meeting.playback.result');
 assert.equal(skipped.params.outcome,'skipped');
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
 await tick();assert.equal(audios.length,0);
 emitSpeech(h,{playback_id:'next',request_id:'next-request',lease_token:'next-lease'});await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'next',chunk_index:0});
 await tick();assert.equal(audios.length,1);audios[0].onended();await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.playback.result').at(-1).params.outcome,'completed');
 assert.ok(!h.capture.stopped);h.meeting.stop();h.meeting.finishCleanup();
});


test('confirmed duplex VAD stops TTS locally before transcription or the network response', async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
 await tick();assert.equal(audios.length,1);assert.ok(!audios[0].paused);
 h.speechDetected();
 assert.ok(audios[0].paused); // synchronous: no ASR and no server round trip
 await tick();
 const requests=h.requests.filter(r=>r.name==='meeting.input.speech_started');
 assert.equal(requests.length,1);assert.equal(requests[0].params.expected_token,'lease');
 assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'));
 assert.ok(!h.capture.stopped && !h.track.stopped);
 h.speechDetected();await tick();assert.equal(h.requests.filter(r=>r.name==='meeting.input.speech_started').length,1);
 interruptSpeech(h);await tick();h.meeting.stop();h.meeting.finishCleanup();
});

test('confirmed VAD during synthesis prevents late audio; paused microphone ignores detections',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 h.speechDetected();await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});
 await tick();assert.equal(audios.length,0);
 h.meeting.stopRecording();h.speechDetected();await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.input.speech_started').length,1);
 h.meeting.stop();h.meeting.finishCleanup();
});


test('speech confirmed while idle also stops a newly arriving speaker before synthesis',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);h.speechDetected();await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.input.speech_started').length,0);
 emitSpeech(h);await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.input.speech_started').length,1);
 assert.equal(h.requests.filter(r=>r.name==='meeting.speech.synthesize').length,0);
 assert.equal(audios.length,0);
 interruptSpeech(h);await tick();h.speechDetected(false);
 h.meeting.stop();h.meeting.finishCleanup();
});


test('JoyAI meeting sends completed browser utterances and drops turns after End', async()=>{
 const h=harness();await ready(h);
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.ready').params.input_mode,'joyai');
 h.turn();h.turn();await tick();await tick();
 const turns=h.requests.filter(r=>r.name==='meeting.input.utterance');
 assert.deepEqual(turns.map(r=>r.params.sequence),[0,1]);
 assert.equal(turns[0].params.audio_data_url,'data:audio/wav;base64,UklGRg==');
 assert.ok(!h.requests.some(r=>r.name==='meeting.input.append'));
 inputEvent(h,'meeting.input.transcript',{transcript:'第一句'});
 await tick();assert.deepEqual(h.drafts,['第一句']);
 h.meeting.stop();h.turn();await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.input.utterance').length,2);
 h.meeting.finishCleanup();
});


test('ASR failures forward their specific cause rather than only the stage name', async()=>{
 const h=harness();await ready(h);const failures=[];
 h.callbacks.error=(code,detail)=>failures.push({code,detail});
 inputEvent(h,'meeting.input.error',{error:'ASR_TIMEOUT: transcription exceeded 60 seconds'});
 assert.deepEqual(failures,[{code:'transcriptionFailed',detail:'ASR_TIMEOUT: transcription exceeded 60 seconds'}]);
 h.meeting.stop();h.meeting.finishCleanup();
});
