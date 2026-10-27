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
  return {meeting,requests,handlers,track,lines,errors,drafts,states,resolvers,capture,
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

test('stop during playback holds the lease and never starts another chunk',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h,{chunks:['First','Second'],chunk_count:2});await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});await tick();
 const oldEnd=audios[0].onended;h.meeting.stopPlayback();oldEnd();await tick();
 assert.ok(audios[0].paused);assert.equal(h.requests.filter(r=>r.name==='meeting.speech.synthesize').length,1);
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.outcome,'stopped');
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.completed_chunks,0);
 h.meeting.stop();h.meeting.finishCleanup();
});

test('failed synthesis holds the queue and duplicate or foreign events do not replay',async()=>{
 const h=harness();await ready(h);emitSpeech(h,{session_id:'other'});emitSpeech(h);emitSpeech(h);await tick();
 h.resolvers['meeting.speech.synthesize']({success:false});await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.speech.synthesize').length,1);
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.outcome,'failed');
 emitSpeech(h,{playback_id:'next'});await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.speech.synthesize').length,1);
 h.meeting.stop();h.meeting.finishCleanup();
});

test('chat events cannot narrate a meeting independently of the granted speech',async()=>{
 const h=harness();await ready(h);
 assert.ok(!h.handlers.has('team.message'));assert.ok(!h.handlers.has('chat.final'));
 assert.ok(!h.requests.some(r=>r.name==='tts.synthesize'));h.meeting.stop();h.meeting.finishCleanup();
});

test('disconnect stops recording and releases microphone',async()=>{
  const h=harness();await ready(h);h.disconnect();
  assert.ok(h.track.stopped);assert.ok(h.capture.stopped);assert.deepEqual(h.errors,['disconnected']);
});

test('cancelling while microphone permission is pending releases the late stream',async()=>{
  const h=harness();let grant;
  navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{grant=resolve;});
  const start=h.meeting.start();h.meeting.stop();
  grant({getTracks:()=>[h.track],getAudioTracks:()=>[h.track]});await start;
  assert.ok(h.track.stopped);assert.equal(h.handlers.size,0);
});

test('a long authorized summary may contain more than 32 bounded chunks',async()=>{
 const h=harness();await ready(h);
 emitSpeech(h,{chunks:Array.from({length:34},()=> 'x'.repeat(430)),chunk_count:34});await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.speech.synthesize').length,1);
 h.meeting.stop();h.resolvers['meeting.speech.synthesize']({success:false});await tick();h.meeting.finishCleanup();
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

test('foreign, duplicate and late input results never enter the conversation',async()=>{
 const h=harness();await ready(h);
 inputEvent(h,'meeting.input.transcript',{stream_id:'old',transcript:'Old'});
 inputEvent(h,'meeting.input.transcript',{meeting_id:'other',transcript:'Foreign'});
 inputEvent(h,'meeting.input.transcript',{transcript:'Once'});
 inputEvent(h,'meeting.input.transcript',{transcript:'Duplicate'});
 const late=h.handlers.get('meeting.input.transcript');h.meeting.stop();
 late({payload:{session_id:'s',meeting_id:'meeting',stream_id:'input',utterance_id:2,transcript:'Late'}});
 assert.deepEqual(h.drafts,['Once']);h.meeting.finishCleanup();
});

test('pausing and resuming the microphone does not hold or stop agent playback',async()=>{
 const h=harness(),audios=audioHarness();await ready(h);emitSpeech(h);await tick();
 h.resolvers['meeting.speech.synthesize']({success:true,audio_base64:'YQ==',playback_id:'playback',chunk_index:0});await tick();
 h.pcm();h.meeting.stopRecording();h.pcm();await tick();
 assert.equal(h.requests.filter(r=>r.name==='meeting.input.append').length,1);
 assert.ok(h.requests.some(r=>r.name==='meeting.input.pause'));assert.equal(h.states.at(-1),'ready');
 assert.ok(!audios[0].paused);assert.ok(!h.requests.some(r=>r.name==='meeting.playback.result'));
 h.meeting.startRecording();await tick();h.pcm();await tick();
 assert.ok(h.requests.some(r=>r.name==='meeting.input.resume'));assert.equal(h.states.at(-1),'recording');
 audios[0].onended();await tick();
 assert.equal(h.requests.find(r=>r.name==='meeting.playback.result').params.outcome,'completed');
 h.meeting.stop();h.meeting.finishCleanup();
});

test('slow transport bounds microphone backlog and stops without sending queued stale audio',async()=>{
 const h=harness();await ready(h);const releases=[];
 const original=h.meeting.transport.request;
 h.meeting.transport.request=(name,...args)=> name==='meeting.input.append'?new Promise(resolve=>{releases.push(resolve);}):original(name,...args);
 h.pcm();await tick();for(let i=0;i<21;i++)h.pcm();
 assert.deepEqual(h.errors,['inputStreamFailed']);assert.ok(h.track.stopped);assert.ok(h.capture.stopped);
 releases.forEach(release=>release({accepted:true}));await tick();assert.equal(h.requests.filter(r=>r.name==='meeting.input.stop').length,1);
 h.meeting.finishCleanup();
});

test('capture initialization cancellation closes resources without opening an input stream',async()=>{
 let release;const h=harness({startCapture:()=>new Promise(resolve=>{release=resolve;})});
 const starting=h.meeting.start();await tick();h.meeting.stop();release();await starting;
 assert.ok(h.track.stopped);assert.ok(h.capture.stopped);assert.ok(!h.requests.some(r=>r.name==='meeting.playback.ready'));
 h.meeting.finishCleanup();
});

test('ASR failure leaves continuous capture running for the next utterance',async()=>{
 const h=harness();await ready(h);inputEvent(h,'meeting.input.error');
 h.pcm();await tick();inputEvent(h,'meeting.input.transcript',{utterance_id:2,transcript:'Recovered'});
 assert.deepEqual(h.errors,['transcriptionFailed']);assert.deepEqual(h.drafts,['Recovered']);assert.equal(h.states.at(-1),'recording');
 h.meeting.stop();h.meeting.finishCleanup();
});

test('chunks pipeline across delayed acknowledgements and Pause waits for all earlier chunks',async()=>{
 const h=harness();await ready(h);const releases=[];const sent=[];const original=h.meeting.transport.request;
 h.meeting.transport.request=(name,...args)=>{
  sent.push(name);
  return name==='meeting.input.append'?new Promise(resolve=>releases.push(resolve)):original(name,...args);
 };
 h.pcm();h.pcm();h.pcm();await tick();assert.equal(releases.length,3);
 h.meeting.stopRecording();await tick();assert.ok(!sent.includes('meeting.input.pause'));
 releases[1]({accepted:true});releases[2]({accepted:true});await tick();assert.ok(!sent.includes('meeting.input.pause'));
 releases[0]({accepted:true});await tick();assert.ok(sent.includes('meeting.input.pause'));
 h.meeting.stop();h.meeting.finishCleanup();
});
