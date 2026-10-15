import assert from 'node:assert/strict';
import test from 'node:test';
import { speechChunks, replyFromEvent, MeetingSession } from '../../../channels/web/frontend/node_modules/.cache/meeting/meetingRuntime.mjs';

const context = {leader:'leader',members:[
  {member_name:'leader',display_name:'Leader'},
  {member_name:'dev',display_name:'小王'},
  {member_name:'ann',display_name:'Ann'},
]};
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

function harness() {
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
  const meeting=new MeetingSession(transport,'s',context,callbacks);
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
  return {meeting,requests,handlers,track,lines,errors,drafts,states,recorders,resolvers,disconnect:()=>onState?.('closed')};
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
