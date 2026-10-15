import assert from 'node:assert/strict';
import test from 'node:test';
import { speechChunks, replyFromEvent, MeetingSession } from '../../../channels/web/frontend/node_modules/.cache/meeting/meetingRuntime.mjs';

const context = {leader:'leader',members:[
  {member_name:'leader',display_name:'Leader'},
  {member_name:'dev',display_name:'小王'},
  {member_name:'ann',display_name:'Ann'},
]};
import { SpeechGate } from '../../../channels/web/frontend/node_modules/.cache/meeting/speechGate.mjs';

const tick = () => new Promise(resolve=>setImmediate(resolve));

test('long replies split into bounded chunks without truncating content', () => {
  const text = 'Report。'.repeat(500);
  const chunks = speechChunks(text);
  assert.ok(chunks.every(chunk => chunk.length <= 800));
  assert.equal(chunks.join(''), text);
});
test('only human-directed replies are spoken in the frontend pass', () => {
  const event = {type:'team.message.p2p',from_member:'dev',to_member:'user',content:'Ready',message_id:'m'};
  assert.equal(replyFromEvent('team.message',{event},'leader').speaker,'dev');
  assert.equal(replyFromEvent('team.message',{event:{...event,to_member:'leader'}},'leader'),null);
  assert.equal(replyFromEvent('team.message',{event:{...event,type:'team.message.broadcast'}},'leader'),null);
  assert.equal(replyFromEvent('chat.final',{content:'Ready',member_name:'dev'},'leader'),null);
});

function harness({startVad = async()=>{}} = {}) {
  const handlers = new Map();
  const requests = [], lines=[], errors=[], drafts=[], states=[];
  const resolvers = {};
  let onState;
  const transport = {
    on:(name,fn)=>{handlers.set(name,fn); return ()=>handlers.delete(name);},
    onStateChange:fn=>{onState=fn;return ()=>{onState=null;};},
    request:async(name,params)=>{
      requests.push({name,params});
      if(name === 'tts.synthesize' || name === 'video.transcribe') return new Promise(resolve=>{resolvers[name]=resolve;});
      return {};
    },
  };
  const callbacks={line:line=>lines.push(line),error:code=>errors.push(code),draft:text=>drafts.push(text),recording:state=>states.push(state),ended:()=>{}};
  let detect, vadError, gate=new SpeechGate();
  const vad={stopped:false,resets:0,start:startVad,reset(){this.resets++;gate=new SpeechGate();},stop(){this.stopped=true;}};
  const meeting=new MeetingSession(transport,'s',context,callbacks,(_stream,onDetection,onError)=>{detect=onDetection;vadError=onError;return vad;});
  const track={stop(){this.stopped=true;},onended:null};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[track],getAudioTracks:()=>[track]})}}});
  const recorders=[];
  globalThis.MediaRecorder=class {
    static isTypeSupported=()=>true;
    state='inactive';mimeType='audio/webm';
    constructor(){recorders.push(this);}
    start(){this.state='recording';}
    stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['audio'])});this.onstop?.();}
  };
  globalThis.FileReader=class {readAsDataURL(){this.result='data:audio/webm;base64,YXVkaW8=';this.onload();}};
  return {meeting,requests,handlers,track,lines,errors,drafts,states,recorders,resolvers,vad,
    detect:(state)=>detect({state}),vadError:()=>vadError(),
    feed:(probability,frames,level=1000)=>{for(let i=0;i<frames;i++)detect(gate.process(probability,level));},
    disconnect:()=>onState?.('closed')};
}

test('ending a meeting discards late synthesis and releases microphone',async()=>{
  const h=harness();let played=0;
  globalThis.Audio=class {constructor(){played++;}};
  await h.meeting.start();
  h.handlers.get('chat.final')({payload:{session_id:'s',content:'Reply',request_id:'r'}});
  await tick();
  assert.equal(h.requests.filter(r=>r.name==='tts.synthesize').length,1);
  h.meeting.stop();h.resolvers['tts.synthesize']({success:true,audio_base64:'YQ==',audio_mime:'audio/mpeg'});
  await tick();assert.equal(played,0);assert.ok(h.track.stopped);assert.equal(h.handlers.size,0);
});

test('starting meeting capture never sends a chat message',async()=>{
  const h=harness();await h.meeting.start();h.meeting.startRecording();
  assert.ok(!h.requests.some(r=>r.name==='chat.send'));h.meeting.stop();
});

test('recording uses existing ASR and leaves transcript for review',async()=>{
  const h=harness();await h.meeting.start();h.meeting.startRecording();
  assert.equal(h.states.at(-1),'recording');h.meeting.stopRecording();await tick();
  assert.equal(h.states.at(-1),'transcribing');
  assert.ok(h.requests.find(r=>r.name==='video.transcribe').params.audio_data_url.startsWith('data:audio/webm'));
  h.resolvers['video.transcribe']({transcript:'Review this'});await tick();
  assert.deepEqual(h.drafts,['Review this']);assert.equal(h.states.at(-1),'ready');
  assert.ok(!h.requests.some(r=>r.name==='chat.send'));h.meeting.stop();
});

test('late ASR after stop cannot modify the next meeting draft',async()=>{
  const h=harness();await h.meeting.start();h.meeting.startRecording();h.meeting.stopRecording();await tick();
  h.meeting.stop();h.resolvers['video.transcribe']({transcript:'Late'});await tick();assert.deepEqual(h.drafts,[]);
});

test('duplicate reply events and other sessions never enqueue extra speech',async()=>{
  const h=harness();await h.meeting.start();
  const event={payload:{session_id:'s',event:{type:'team.message.p2p',from_member:'dev',to_member:'user',content:'Ready',message_id:'m'}}};
  h.handlers.get('team.message')({payload:{...event.payload,session_id:'other'}});
  h.handlers.get('team.message')(event);h.handlers.get('team.event')(event);await tick();
  assert.equal(h.lines.length,1);assert.equal(h.requests.filter(r=>r.name==='tts.synthesize').length,1);
  assert.equal(h.requests.at(-1).params.member_name,undefined); // Backend voice mapping is deferred.
  h.meeting.stop();h.resolvers['tts.synthesize']({success:true,audio_base64:'YQ=='});await tick();
});

test('disconnect stops recording and releases microphone',async()=>{
  const h=harness();await h.meeting.start();h.meeting.startRecording();h.disconnect();
  assert.ok(h.track.stopped);assert.equal(h.recorders[0].state,'inactive');assert.deepEqual(h.errors,['disconnected']);
});

test('cancelling while microphone permission is pending releases the late stream',async()=>{
  const h=harness();let grant;
  navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{grant=resolve;});
  const start=h.meeting.start();h.meeting.stop();
  grant({getTracks:()=>[h.track],getAudioTracks:()=>[h.track]});await start;
  assert.ok(h.track.stopped);assert.equal(h.handlers.size,0);
});

test('confirmed speech followed by 640ms silence automatically transcribes once', async () => {
  const h = harness();
  await h.meeting.start();
  h.meeting.startRecording();
  try {
    h.feed(0.1, 100, 14000); // Loud background noise and initial silence are not utterances.
    assert.equal(h.recorders[0].state, 'recording');
    assert.equal(h.requests.length, 0);
    h.feed(0.99, 8);
    h.feed(0.1, 19);
    assert.equal(h.recorders[0].state, 'recording'); // 608ms pause is below the shared gate's end threshold.
    h.feed(0.99, 8);
    h.feed(0.1, 20);
    await tick();
    assert.equal(h.recorders[0].state, 'inactive');
    assert.equal(h.states.at(-1), 'transcribing');
    h.detect('ended');
    h.meeting.stopRecording();
    await tick();
    assert.equal(h.requests.filter((r) => r.name === 'video.transcribe').length, 1);
    h.resolvers['video.transcribe']({ transcript: 'Finished speaking' });
    await tick();
    assert.deepEqual(h.drafts, ['Finished speaking']);
    assert.equal(h.states.at(-1), 'ready');
    assert.ok(!h.requests.some((r) => r.name === 'chat.send'));
  } finally {
    h.meeting.stop();
  }
});

test('each recording resets VAD and ignores an end event without new speech', async () => {
  const h = harness();
  await h.meeting.start();
  h.meeting.startRecording();
  try {
    h.feed(0.99, 8);
    h.feed(0.1, 20);
    await tick();
    h.resolvers['video.transcribe']({ transcript: 'First' });
    await tick();
    h.meeting.startRecording();
    assert.equal(h.vad.resets, 2);
    h.detect('ended');
    assert.equal(h.recorders[1].state, 'recording');
    h.feed(0.99, 8);
    h.feed(0.1, 20);
    await tick();
    assert.equal(h.requests.filter((r) => r.name === 'video.transcribe').length, 2);
  } finally {
    h.meeting.stop();
  }
});

test('30 seconds with no confirmed speech discards the silent recording', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness();
  await h.meeting.start();
  h.meeting.startRecording();
  try {
    h.feed(0.1, 100, 14000);
    t.mock.timers.tick(30000);
    await tick();
    assert.equal(h.recorders[0].state, 'inactive');
    assert.equal(h.states.at(-1), 'ready');
    assert.equal(h.requests.length, 0);
    assert.deepEqual(h.errors, []);
  } finally {
    h.meeting.stop();
  }
});

test('continuous speech retains the 30-second recording limit', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness();
  await h.meeting.start();
  h.meeting.startRecording();
  try {
    h.feed(0.99, 8);
    t.mock.timers.tick(30000);
    await tick();
    assert.equal(h.requests.filter((r) => r.name === 'video.transcribe').length, 1);
  } finally {
    h.meeting.stop();
  }
});

test('unavailable VAD reports manual fallback and still allows ASR', async () => {
  const h = harness({
    startVad: async () => {
      throw new Error('model unavailable');
    },
  });
  await h.meeting.start();
  h.meeting.startRecording();
  try {
    assert.deepEqual(h.errors, ['vadUnavailable']);
    assert.equal(h.vad.stopped, true);
    h.meeting.stopRecording();
    await tick();
    assert.equal(h.requests.filter((r) => r.name === 'video.transcribe').length, 1);
  } finally {
    h.meeting.stop();
  }
});

test('VAD failure during recording preserves manual finish and meeting cleanup', async () => {
  const h = harness();
  await h.meeting.start();
  h.meeting.startRecording();
  h.vadError();
  assert.equal(h.vad.stopped, true);
  assert.deepEqual(h.errors, ['vadUnavailable']);
  h.meeting.stopRecording();
  await tick();
  assert.equal(h.requests.at(-1).name, 'video.transcribe');
  h.meeting.stop();
  assert.equal(h.track.stopped, true);
  h.detect('ended');
  await tick();
  assert.equal(h.requests.length, 1);
});

test('cancelling while VAD starts releases resources and never records', async () => {
  let ready;
  const h = harness({
    startVad: () =>
      new Promise((resolve) => {
        ready = resolve;
      }),
  });
  const start = h.meeting.start();
  await tick();
  h.meeting.stop();
  ready();
  await start;
  h.meeting.startRecording();
  assert.equal(h.vad.stopped, true);
  assert.equal(h.track.stopped, true);
  assert.equal(h.recorders.length, 0);
  assert.deepEqual(h.errors, []);
});
