import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import test from 'node:test';

const frontend = fileURLToPath(new URL('../../../channels/web/frontend/', import.meta.url));
const require = createRequire(path.join(frontend, 'package.json'));
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { JSDOM } = require('jsdom');
const output = path.join(frontend, 'node_modules/.cache/meeting/meetingAction.cjs');
await require('esbuild').build({
  stdin: { contents: `
    export {TaskMeetingAction} from ${JSON.stringify(fileURLToPath(new URL('../frontend/TaskMeetingAction.tsx', import.meta.url)))};
    export {TaskMeetingRuntime} from ${JSON.stringify(fileURLToPath(new URL('../frontend/TaskMeetingRuntime.tsx', import.meta.url)))};
    export {setTeamMeetingEnabled} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/featureFlag.ts'))};
    export {requests,meetingServer,webClient,emit} from 'meeting-test-transport';
    export {hasMeetingAudioOwner} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/playbackOwnership.ts'))};
    export {rememberGroupChatInput,defaultGroupChatInput} from ${JSON.stringify(path.join(frontend, 'src/features/groupChatInput.ts'))};
  `, resolveDir: frontend },
  bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', packages: 'external',
  outfile: output, loader: { '.css': 'empty' },
  plugins: [{ name: 'meeting-action-test-host', setup(build) {
    build.onResolve({filter:/\/meetingVad$/},()=>({path:'vad',namespace:'meeting-test'}));
    build.onResolve({filter:/react-i18next$/},()=>({path:'i18n',namespace:'meeting-test'}));
    build.onResolve({filter:/stores\/sessionStore$/},()=>({path:'sessions',namespace:'meeting-test'}));
    build.onResolve({filter:/services\/webClient$|^meeting-test-transport$/},()=>({path:'transport',namespace:'meeting-test'}));
    build.onResolve({filter:/Toast\/toastStore$/},()=>({path:'toast',namespace:'meeting-test'}));
    build.onLoad({filter:/.*/,namespace:'meeting-test'},({path})=>({loader:'js',contents: {
      vad: `export class MeetingVad {async start(){}reset(){}stop(){}}`,
      i18n: `export const useTranslation=()=>({t:key=>key,i18n:{language:'en'}});`,
      sessions: `const runtime={mode:'team',teamLeaderMemberIds:['leader'],teamMembers:[]};const state={runtimes:{s:runtime},getRuntime:()=>runtime};export const useSessionStore=fn=>fn(state);useSessionStore.getState=()=>state;`,
      transport: `
        export const requests=[];
        export const meetingServer={state:'active',acceptInputs:true,failInput:false,failEnd:false,failReady:false};
        const handlers=new Map();
        export function emit(name,payload){for(const fn of handlers.get(name)||[])fn({payload});}
        export const webClient={
          getState:()=> 'ready',
          on:(name,fn)=>{if(!handlers.has(name))handlers.set(name,new Set());handlers.get(name).add(fn);return()=>handlers.get(name).delete(fn);},
          onStateChange:()=>()=>{},
          request:async(name,params)=>{
            requests.push({name,params});
            if(name==='meeting.end' && meetingServer.failEnd) throw new Error('unconfirmed cleanup');
            if(name==='meeting.status' && meetingServer.failEnd) return {state:'cleanup_failed'};
            if(name==='meeting.playback.ready' && meetingServer.failReady) throw new Error('unavailable');
            if(name==='chat.send'&&meetingServer.acceptInputs)queueMicrotask(()=>{
              if(meetingServer.failInput)emit('chat.error',{session_id:params.session_id,request_id:params.request_id,error:'delivery failed'});
              else emit('team.group_message.accepted',{session_id:params.session_id,client_message_id:params.client_message_id});
            });
            return name==='config.get'?{team_meeting_enabled:true}:name==='video.transcribe'?{transcript:'Please review the layout.'}:name==='meeting.start'||name==='meeting.status'?{state:meetingServer.state,meeting_id:meetingServer.state==='active'?'meeting-one':undefined}:name==='meeting.end'||name==='meeting.release'?{state:'idle'}:name==='meeting.playback.ready'?{ready:true}:{};
          },
        };`,
      toast: `export const toast={open:()=>{}};`,
    }[path]}));
  }}],
});
const {TaskMeetingAction,TaskMeetingRuntime,setTeamMeetingEnabled,requests,meetingServer,webClient,emit,rememberGroupChatInput,defaultGroupChatInput,hasMeetingAudioOwner}=require(output);

async function mount(extra={}) {
  const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
  Object.assign(globalThis,{window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true});
  const track={stopped:false,stop(){this.stopped=true;},onended:null};let microphoneCalls=0;
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>{microphoneCalls++;return{getTracks:()=>[track],getAudioTracks:()=>[track]};}}}});
  globalThis.MediaRecorder=class {
    static isTypeSupported=()=>true;state='inactive';mimeType='audio/webm';
    start(){this.state='recording';}stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['audio'])});this.onstop?.();}
  };
  globalThis.FileReader=class {readAsDataURL(){this.result='data:audio/webm;base64,YQ==';this.onload();}};
  setTeamMeetingEnabled(false);requests.length=0;meetingServer.state='active';meetingServer.acceptInputs=true;meetingServer.failInput=false;meetingServer.failEnd=false;meetingServer.failReady=false;
  const drafts=[],capture=[],messages=[];
  const onSendMessage=async(text,options)=>{
    messages.push(text);
    const requestId=`chat-${messages.length}`;
    options.groupChat.onRequestId?.(requestId);
    await webClient.request('chat.send',{session_id:'s',content:text,type:'group_chat',body:text,client_message_id:options.groupChat.clientMessageId,mentions:options.groupChat.mentions,meeting_mode:options.groupChat.meeting,request_id:requestId});
    return true;
  };
  const root=createRoot(document.getElementById('root'));
  const props={fallback:React.createElement('button',{'data-testid':'normal-send'},'Send'),eligible:false,teamEligible:true,sessionId:'s',ensureSession:async()=> 's',onTranscript:text=>drafts.push(text),onMicrophoneActiveChange:value=>capture.push(value),labels:{start:'',starting:'',stop:''},...extra};
  const render=(key)=>root.render(React.createElement(React.Fragment,null,React.createElement(TaskMeetingRuntime,{sessionId:'s',key:'runtime',onSendMessage}),React.createElement(TaskMeetingAction,{...props,key})));
  await act(async()=>render('welcome')); 
  return {dom,track,drafts,capture,messages,root,microphoneCalls:()=>microphoneCalls,
    rerenderComposer:async()=>{await act(async()=>render('conversation'));},
    updateProps:async(update)=>{Object.assign(props,update);await act(async()=>render('welcome'));},
    find:id=>document.querySelector(`[data-testid="${id}"]`),
    click:async id=>{await act(async()=>{document.querySelector(`[data-testid="${id}"]`).click();});},
    close:async()=>{await act(async()=>root.unmount());dom.window.close();},
  };
}

test('meeting initializes visibly and sends speech automatically through the normal sender',async()=>{
  const h=await mount();
  try {
    const send=h.find('normal-send');
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-pressed'),'true');
    assert.equal(h.find('chat-panel-input-meeting-record').getAttribute('data-variant'),'recording');
    await h.click('chat-panel-input-meeting-record');
    assert.deepEqual(h.drafts,[]);
    assert.deepEqual(h.messages,['meeting.startMessage','Please review the layout.']);
    assert.equal(h.find('normal-send'),send);
    const inputs=requests.filter(request=>request.name==='chat.send');
    assert.equal(inputs.length,2);
    assert.deepEqual(inputs[0].params.mentions,[]);
    assert.deepEqual(inputs[1].params.mentions,['leader']);
    assert.ok(inputs.every(input=>input.params.meeting_mode===true));
    await h.click('chat-panel-input-meeting');
    assert.ok(h.track.stopped);assert.equal(h.capture.at(-1),false);
    assert.ok(requests.some(request=>request.name==='meeting.end'&&request.params.meeting_id==='meeting-one'));
  } finally {await h.close();}
});

test('turning the Experimental switch off stops an active meeting',async()=>{
  const h=await mount();
  try {
    await h.click('chat-panel-input-meeting');
    await act(async()=>setTeamMeetingEnabled(false));
    assert.equal(h.find('chat-panel-input-meeting'),null);
    assert.ok(h.track.stopped);assert.ok(h.find('normal-send'));
  } finally {await h.close();}
});

test('session creation disables End until the meeting becomes active',async()=>{
  let ready;
  const h=await mount({ensureSession:()=>new Promise(resolve=>{ready=resolve;})});
  try {
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'starting');
    assert.equal(h.find('chat-panel-input-meeting').disabled,true);
    await h.click('chat-panel-input-meeting');
    await act(async()=>ready('s'));
    assert.equal(h.microphoneCalls(),1);
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'active');
    assert.equal(h.find('chat-panel-input-meeting').disabled,false);
  } finally {await h.close();}
});

test('pending Team readiness can be cancelled without opening the microphone',async()=>{
  const h=await mount();
  try {
    meetingServer.state='starting';
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'starting');
    assert.ok(h.find('chat-panel-meeting-waiting'));
    assert.equal(h.microphoneCalls(),0);
    assert.equal(h.find('chat-panel-input-meeting').disabled,false);
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
    assert.ok(requests.some(request=>request.name==='meeting.release'));
    assert.ok(!requests.some(request=>request.name==='meeting.end'));
    await act(async()=>{
      meetingServer.state='idle';
      await new Promise(resolve=>setTimeout(resolve,1100));
    });
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
    assert.ok(!requests.some(request=>request.name==='meeting.end'));
    assert.equal(h.microphoneCalls(),0);
  } finally {await h.close();}
});

test('leaving during startup releases only that startup without calling End',async()=>{
  const h=await mount();
  meetingServer.state='starting';
  await h.click('chat-panel-input-meeting');
  const start=requests.find(request=>request.name==='meeting.start');
  assert.ok(start.params.startup_id);
  await h.close();
  const release=requests.find(request=>request.name==='meeting.release');
  assert.equal(release.params.startup_id,start.params.startup_id);
  assert.ok(!requests.some(request=>request.name==='meeting.end'));
  assert.equal(h.microphoneCalls(),0);
});

test('meeting capture starts when the normal Team runtime becomes ready',async()=>{
  const h=await mount();
  try {
    meetingServer.state='starting';
    await h.click('chat-panel-input-meeting');
    assert.equal(h.microphoneCalls(),0);
    await act(async()=>{
      meetingServer.state='active';
      await new Promise(resolve=>setTimeout(resolve,1100));
    });
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'active');
    assert.equal(h.microphoneCalls(),1);
  } finally {await h.close();}
});

 test('meeting capture survives the welcome composer becoming a conversation',async()=>{
  const h=await mount();
  try {
    await h.click('chat-panel-input-meeting');
    await h.rerenderComposer();
    assert.equal(h.track.stopped,false);
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-pressed'),'true');
    await h.click('chat-panel-input-meeting-record');
    assert.deepEqual(h.drafts,[]);
    assert.deepEqual(h.messages,['meeting.startMessage','Please review the layout.']);
  } finally {await h.close();}
});


test('one click on a new Agent task selects Team mode before creating and recording',async()=>{
  const order=[];let ready;
  const h=await mount({sessionId:'new',teamEligible:false,onSelectTeamMode:()=>{order.push('team');},ensureSession:()=>{order.push('session');return new Promise(resolve=>{ready=resolve;});}});
  try {
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-label'),'meeting.task.start');
    await h.click('chat-panel-input-meeting');
    assert.deepEqual(order,['team','session']);
    assert.equal(h.microphoneCalls(),0);
    await h.updateProps({teamEligible:true,onSelectTeamMode:undefined});
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'starting');
    await h.updateProps({sessionId:'s'});
    await act(async()=>ready('s'));
    assert.equal(h.microphoneCalls(),1);
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-pressed'),'true');
    assert.equal(h.find('chat-panel-input-meeting-record').getAttribute('data-variant'),'recording');
    assert.ok(h.find('normal-send'));
  } finally {await h.close();}
});

test('existing Agent conversations do not offer Team meetings',async()=>{
  const h=await mount({teamEligible:false});
  try {
    assert.equal(h.find('chat-panel-input-meeting'),null);
    assert.ok(h.find('normal-send'));assert.equal(h.microphoneCalls(),0);
  } finally {await h.close();}
});


test('runtime readiness alone does not enable recording before public initialization is accepted',async()=>{
  const h=await mount();
  try {
    meetingServer.acceptInputs=false;
    await h.click('chat-panel-input-meeting');
    const initial=requests.find(request=>request.name==='chat.send');
    assert.ok(initial.params.client_message_id);
    assert.equal(h.microphoneCalls(),0);
    assert.ok(!requests.some(request=>request.name==='meeting.start'));
    await act(async()=>{
      emit('team.runtime_ready',{session_id:'s'});
      emit('team.group_message.accepted',{session_id:'other',client_message_id:initial.params.client_message_id});
      emit('team.group_message.accepted',{session_id:'s',client_message_id:'other'});
    });
    assert.equal(h.microphoneCalls(),0);
    await act(async()=>emit('team.group_message.accepted',{
      session_id:'s',message:{client_message_id:initial.params.client_message_id},
    }));
    assert.equal(h.microphoneCalls(),1);
    assert.ok(requests.findIndex(request=>request.name==='chat.send')<requests.findIndex(request=>request.name==='meeting.start'));
  } finally {await h.close();}
});

test('initialization errors prevent recording and retries reuse the public message identity',async()=>{
  const h=await mount();
  try {
    meetingServer.failInput=true;
    await h.click('chat-panel-input-meeting');
    assert.equal(h.microphoneCalls(),0);
    assert.ok(!requests.some(request=>request.name==='meeting.start'));
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
    const first=requests.find(request=>request.name==='chat.send');
    meetingServer.failInput=false;meetingServer.failEnd=false;meetingServer.failReady=false;
    await h.click('chat-panel-input-meeting');
    const inputs=requests.filter(request=>request.name==='chat.send');
    assert.equal(inputs[1].params.client_message_id,first.params.client_message_id);
    assert.equal(h.microphoneCalls(),1);
  } finally {await h.close();}
});

test('leaving while initialization is unaccepted ignores late acceptance and never opens the microphone',async()=>{
  const h=await mount();
  meetingServer.acceptInputs=false;
  await h.click('chat-panel-input-meeting');
  const initial=requests.find(request=>request.name==='chat.send');
  await h.close();
  emit('team.group_message.accepted',{session_id:'s',client_message_id:initial.params.client_message_id});
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(h.microphoneCalls(),0);
  assert.ok(!requests.some(request=>request.name==='meeting.start'));
});


test('only accepted group conversations get default routing and each utterance gets its own identity',()=>{
  const dom=new JSDOM('',{url:'http://localhost'});
  const previous=globalThis.window;
  globalThis.window=dom.window;
  try {
    const id=`group-${crypto.randomUUID()}`;
    assert.equal(defaultGroupChatInput(id,'leader'),undefined);
    rememberGroupChatInput(id);
    assert.equal(window.sessionStorage.getItem(`group-chat-input:${id}`),'1');
    const first=defaultGroupChatInput(id,'captain');
    const next=defaultGroupChatInput(id,'new-leader');
    assert.equal(first.sessionId,id);
    assert.deepEqual(first.mentions,['captain']);
    assert.deepEqual(next.mentions,['new-leader']);
    assert.notEqual(first.clientMessageId,next.clientMessageId);
    assert.equal(defaultGroupChatInput(`${id}-ordinary`,'leader'),undefined);
  } finally {dom.window.close();globalThis.window=previous;}
});

test('an unrelated request error cannot end startup but a matching mode conflict fails before recording',async()=>{
  const h=await mount();
  try {
    meetingServer.acceptInputs=false;
    await h.click('chat-panel-input-meeting');
    const initial=requests.find(request=>request.name==='chat.send');
    await act(async()=>emit('chat.error',{session_id:'s',request_id:'unrelated',error:'error'}));
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'starting');
    await act(async()=>emit('chat.error',{
      session_id:'s',request_id:initial.params.request_id,error:'Team conversation mode cannot change within a session',
    }));
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
    assert.equal(h.microphoneCalls(),0);
    assert.ok(!requests.some(request=>request.name==='meeting.start'));
  } finally {await h.close();}
});

test('paused and failed speech remain visible with an available End control',async()=>{
 const h=await mount();try {
  await h.click('chat-panel-input-meeting');
  await act(async()=>emit('meeting.speech.error',{session_id:'s',meeting_id:'meeting-one',error:'provider failed'}));
  assert.equal(h.find('chat-panel-meeting-playback').getAttribute('data-variant'),'failed');
  assert.equal(h.find('chat-panel-input-meeting').disabled,false);
  await h.click('chat-panel-input-meeting-stop-audio');
  assert.equal(h.find('chat-panel-meeting-playback').getAttribute('data-variant'),'paused');
  await h.click('chat-panel-input-meeting');
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
 }finally{await h.close();}
});

test('unconfirmed cleanup preserves End retry and prevents a replacement meeting',async()=>{
 const h=await mount();try {
  await h.click('chat-panel-input-meeting');meetingServer.failEnd=true;
  await h.click('chat-panel-input-meeting');
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'cleanup_failed');
  assert.ok(h.find('chat-panel-meeting-cleanup-error'));
  assert.ok(hasMeetingAudioOwner('s'));
  assert.equal(h.find('chat-panel-input-meeting').disabled,false);
  await h.updateProps({sessionId:'another-session'});
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'cleanup_failed');
  meetingServer.failEnd=false;await h.click('chat-panel-input-meeting');
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
  assert.equal(requests.filter(r=>r.name==='meeting.start').length,1);
  assert.equal(hasMeetingAudioOwner('s'),false);
 }finally{await h.close();}
});

test('failed playback readiness releases media and closes the server meeting',async()=>{
 const h=await mount();try {
  meetingServer.failReady=true;await h.click('chat-panel-input-meeting');
  assert.ok(h.track.stopped);
  assert.ok(requests.some(r=>r.name==='meeting.end'));
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
 }finally{await h.close();}
});

test('failed cleanup can be retried after the Task runtime remounts',async()=>{
 const first=await mount();
 await first.click('chat-panel-input-meeting');meetingServer.failEnd=true;
 await first.click('chat-panel-input-meeting');
 assert.equal(first.find('chat-panel-input-meeting').getAttribute('data-variant'),'cleanup_failed');
 await first.close();
 const second=await mount();try {
  assert.equal(second.find('chat-panel-input-meeting').getAttribute('data-variant'),'cleanup_failed');
  await second.click('chat-panel-input-meeting');
  assert.equal(second.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
  assert.ok(requests.some(r=>r.name==='meeting.end'&&r.params.meeting_id==='meeting-one'));
 }finally{await second.close();}
});
