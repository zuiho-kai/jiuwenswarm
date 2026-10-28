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
    export {setMeetingRoutingMode} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/routingMode.ts'))};
    export {requests,meetingServer,webClient,emit} from 'meeting-test-transport';
    export {hasMeetingAudioOwner} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/playbackOwnership.ts'))};
    export {isTaskInputBlocked} from ${JSON.stringify(path.join(frontend, 'src/applicationPlugins/taskInputReadiness.ts'))};
    export {rememberGroupChatInput,defaultGroupChatInput} from ${JSON.stringify(path.join(frontend, 'src/features/groupChatInput.ts'))};
  `, resolveDir: frontend },
  bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', packages: 'external',
  outfile: output, loader: { '.css': 'empty' },
  plugins: [{ name: 'meeting-action-test-host', setup(build) {
    build.onResolve({filter:/\/meetingCapture$/},()=>({path:'capture',namespace:'meeting-test'}));
    build.onResolve({filter:/react-i18next$/},()=>({path:'i18n',namespace:'meeting-test'}));
    build.onResolve({filter:/stores\/sessionStore$/},()=>({path:'sessions',namespace:'meeting-test'}));
    build.onResolve({filter:/services\/webClient$|^meeting-test-transport$/},()=>({path:'transport',namespace:'meeting-test'}));
    build.onResolve({filter:/Toast\/toastStore$/},()=>({path:'toast',namespace:'meeting-test'}));
    build.onLoad({filter:/.*/,namespace:'meeting-test'},({path})=>({loader:'js',contents: {
      capture: `export class MeetingPcmCapture {async start(){}stop(){}}`,
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
            if(name==='meeting.playback.ready' && meetingServer.beforeReady) await meetingServer.beforeReady();
            if(name==='chat.send'&&meetingServer.acceptInputs)queueMicrotask(()=>{
              if(meetingServer.failInput)emit('chat.error',{session_id:params.session_id,request_id:params.request_id,error:'delivery failed'});
              else emit('team.group_message.accepted',{session_id:params.session_id,client_message_id:params.client_message_id});
            });
            if(name==='meeting.input.pause')queueMicrotask(()=>emit('meeting.input.transcript',{session_id:'s',meeting_id:'meeting-one',stream_id:'input',utterance_id:1,transcript:'Please review the layout.'}));
            return name==='meeting.input.pause'||name==='meeting.input.resume'?{accepted:true}:name==='config.get'?{team_meeting_enabled:true}:name==='meeting.start'||name==='meeting.status'?{state:meetingServer.state,meeting_id:meetingServer.state==='active'?'meeting-one':undefined}:name==='meeting.end'||name==='meeting.release'?{state:'idle'}:name==='meeting.playback.ready'?{ready:true,stream_id:'input'}:{};
          },
        };`,
      toast: `export const toast={open:()=>{}};`,
    }[path]}));
  }}],
});
const {TaskMeetingAction,TaskMeetingRuntime,setTeamMeetingEnabled,setMeetingRoutingMode,requests,meetingServer,webClient,emit,rememberGroupChatInput,defaultGroupChatInput,hasMeetingAudioOwner,isTaskInputBlocked}=require(output);

async function mount(extra={}) {
  const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
  Object.assign(globalThis,{window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true});
  setMeetingRoutingMode('leader');
  const track={stopped:false,stop(){this.stopped=true;},onended:null};let microphoneCalls=0;
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>{microphoneCalls++;return{getTracks:()=>[track],getAudioTracks:()=>[track]};}}}});
  setTeamMeetingEnabled(false);requests.length=0;meetingServer.state='active';meetingServer.acceptInputs=true;meetingServer.failInput=false;meetingServer.failEnd=false;meetingServer.failReady=false;
  meetingServer.beforeReady=null;
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

test('None routing sends speech without mentions and stays fixed for the meeting',async()=>{
  const h=await mount();
  try {
    setMeetingRoutingMode('none');
    await h.click('chat-panel-input-meeting');
    setMeetingRoutingMode('leader');
    await h.click('chat-panel-input-meeting-record');
    const inputs=requests.filter(request=>request.name==='chat.send');
    assert.equal(inputs.length,2);
    assert.deepEqual(inputs[1].params.mentions,[]);
    assert.equal(inputs[1].params.content,'Please review the layout.');
  } finally {await h.close();}
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

test('input stays blocked through acceptance and playback preparation, then unlocks',async()=>{
 const h=await mount();let finishReady;
 try {
  meetingServer.acceptInputs=false;
  meetingServer.beforeReady=()=>new Promise(resolve=>{finishReady=resolve;});
  await h.click('chat-panel-input-meeting');
  assert.equal(isTaskInputBlocked('s'),true);
  assert.equal(isTaskInputBlocked('unrelated'),false);
  const initial=requests.find(r=>r.name==='chat.send');
  await act(async()=>emit('team.group_message.accepted',{session_id:'s',client_message_id:initial.params.client_message_id}));
  assert.equal(h.microphoneCalls(),1);
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'starting');
  assert.equal(isTaskInputBlocked('s'),true);
  assert.equal(h.find('chat-panel-input-meeting-record'),null);
  await act(async()=>finishReady());
  assert.equal(isTaskInputBlocked('s'),false);
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'active');
 }finally{await h.close();}
});
