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
    export {MeetingPanel} from ${JSON.stringify(fileURLToPath(new URL('../frontend/MeetingPanel.tsx', import.meta.url)))};
    export {setTeamMeetingEnabled} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/featureFlag.ts'))};
    export {setMeetingRoutingMode} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/routingMode.ts'))};
    export {requests,meetingServer,webClient,emit} from 'meeting-test-transport';
    export {conversationMessages} from 'meeting-test-chat';
    export {hasMeetingAudioOwner} from ${JSON.stringify(path.join(frontend, 'src/features/teamMeeting/playbackOwnership.ts'))};
    export {isTaskInputBlocked} from ${JSON.stringify(path.join(frontend, 'src/applicationPlugins/taskInputReadiness.ts'))};
    export {rememberGroupChatInput,defaultGroupChatInput} from ${JSON.stringify(path.join(frontend, 'src/features/groupChatInput.ts'))};
  `, resolveDir: frontend },
  bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', packages: 'external',
  outfile: output, loader: { '.css': 'empty' },
  plugins: [{ name: 'meeting-action-test-host', setup(build) {
    build.onResolve({filter:/\/meetingCapture$/},()=>({path:'capture',namespace:'meeting-test'}));
    build.onResolve({filter:/react-i18next$/},()=>({path:'i18n',namespace:'meeting-test'}));
    build.onResolve({filter:/stores\/chatStore$|^meeting-test-chat$/},()=>({path:'chat',namespace:'meeting-test'}));
    build.onResolve({filter:/stores\/workspaceStore$/},()=>({path:'workspace',namespace:'meeting-test'}));
    build.onResolve({filter:/stores\/sessionStore$/},()=>({path:'sessions',namespace:'meeting-test'}));
    build.onResolve({filter:/services\/webClient$|^meeting-test-transport$/},()=>({path:'transport',namespace:'meeting-test'}));
    build.onResolve({filter:/Toast\/toastStore$/},()=>({path:'toast',namespace:'meeting-test'}));
    build.onLoad({filter:/.*/,namespace:'meeting-test'},({path})=>({loader:'js',contents: {
      capture: `export class MeetingPcmCapture {async start(){}stop(){}}`,
      i18n: `export const useTranslation=()=>({t:key=>key,i18n:{language:'en'}});`,
      chat: `export const conversationMessages=[];export const useChatStore={getState:()=>({getRuntime:()=>({messages:conversationMessages}),addMessage:(sessionId,message)=>conversationMessages.push({...message,sessionId})})};`,
      workspace: `export const useWorkspaceStore={getState:()=>({renameSession:async(sessionId,title)=>{globalThis.meetingRenamedTitle=title;}})};`,
      sessions: `const runtime={mode:'team',teamLeaderMemberIds:['leader'],teamMembers:[{member_id:'analyst',name:'Analyst',role:'teammate',status:'ready'}]};const state={sessions:[],runtimes:{s:runtime},getRuntime:()=>runtime};export const useSessionStore=fn=>fn(state);useSessionStore.getState=()=>state;`,
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
            return name==='meeting.queue.control'?{decision:'Use Canvas',pending:[]}:name==='meeting.input.pause'||name==='meeting.input.resume'?{accepted:true}:name==='config.get'?{team_meeting_enabled:true}:name==='meeting.start'||name==='meeting.status'?{state:meetingServer.state,meeting_id:meetingServer.state==='active'?'meeting-one':undefined}:name==='meeting.end'||name==='meeting.release'?{state:'idle'}:name==='meeting.playback.ready'?{ready:true,stream_id:'input'}:{};
          },
        };`,
      toast: `export const toast={open:()=>{}};`,
    }[path]}));
  }}],
});
const {conversationMessages,TaskMeetingAction,TaskMeetingRuntime,MeetingPanel,setTeamMeetingEnabled,setMeetingRoutingMode,requests,meetingServer,webClient,emit,rememberGroupChatInput,defaultGroupChatInput,hasMeetingAudioOwner,isTaskInputBlocked}=require(output);

async function mount(extra={}) {
  const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
  Object.assign(globalThis,{window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true});
  setMeetingRoutingMode('leader');
  const track={stopped:false,stop(){this.stopped=true;},onended:null};let microphoneCalls=0;
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>{microphoneCalls++;return{getTracks:()=>[track],getAudioTracks:()=>[track]};}}}});
  conversationMessages.length=0;
  setTeamMeetingEnabled(false);requests.length=0;meetingServer.state='active';meetingServer.acceptInputs=true;meetingServer.failInput=false;meetingServer.failEnd=false;meetingServer.failReady=false;
  meetingServer.beforeReady=null;
  const drafts=[],capture=[],messages=[];
  let primaryStopAction = null;
  const onSendMessage=async(text,options)=>{
    messages.push(text);
    const requestId=`chat-${messages.length}`;
    options.groupChat.onRequestId?.(requestId);
    await webClient.request('chat.send',{session_id:'s',content:text,type:'group_chat',body:text,client_message_id:options.groupChat.clientMessageId,mentions:options.groupChat.mentions,meeting_mode:options.groupChat.meeting,meeting_startup:options.groupChat.meetingStartup,request_id:requestId});
    return true;
  };
  const root=createRoot(document.getElementById('root'));
  const props={fallback:React.createElement('button',{'data-testid':'normal-send'},'Send'),eligible:false,teamEligible:true,sessionId:'s',ensureSession:async()=> 's',onTranscript:text=>drafts.push(text),onMicrophoneActiveChange:value=>capture.push(value),onStopActionChange:action=>{primaryStopAction=action;},labels:{start:'',starting:'',stop:''},...extra};
  const render=(key)=>root.render(React.createElement(React.Fragment,null,React.createElement(TaskMeetingRuntime,{sessionId:'s',key:'runtime',onSendMessage}),React.createElement(TaskMeetingAction,{...props,key}),React.createElement(MeetingPanel,{sessionId:props.sessionId})));
  await act(async()=>render('welcome')); 
  return {dom,track,drafts,capture,messages,root,microphoneCalls:()=>microphoneCalls,
    rerenderComposer:async()=>{await act(async()=>render('conversation'));},
    updateProps:async(update)=>{Object.assign(props,update);await act(async()=>render('welcome'));},
    primaryStop:()=>primaryStopAction,
    find:id=>document.querySelector(`[data-testid="${id}"]`),
    click:async id=>{await act(async()=>{document.querySelector(`[data-testid="${id}"]`).click();});},
    close:async()=>{await act(async()=>root.unmount());dom.window.close();},
  };
}

test('meeting initializes visibly and sends speech automatically through the normal sender',async()=>{
  const h=await mount();
  try {
    const send=h.find('normal-send');
    globalThis.meetingRenamedTitle=undefined;
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-pressed'),'true');
    assert.equal(h.find('team-area-meeting-status').getAttribute('data-variant'),'active');
    assert.equal(globalThis.meetingRenamedTitle,undefined);
    assert.equal(requests.find(request=>request.name==='chat.send').params.meeting_startup,true);
    assert.equal(h.find('team-area-meeting-addressed-agents').textContent,'meeting.panel.noAddressedAgents');
    assert.equal(h.find('team-area-meeting-speech-route').textContent,'@meeting.leader');
    await act(async()=>emit('meeting.addressed_members',{session_id:'another',meeting_id:'meeting-one',addressed_members:['analyst']}));
    assert.equal(h.find('team-area-meeting-addressed-agents').textContent,'meeting.panel.noAddressedAgents');
    await act(async()=>emit('meeting.addressed_members',{session_id:'s',meeting_id:'meeting-one',addressed_members:['analyst']}));
    assert.equal(h.find('team-area-meeting-addressed-agents').textContent,'@Analyst');
    await act(async()=>emit('meeting.addressed_members',{session_id:'s',meeting_id:'meeting-one',addressed_members:[]}));
    assert.equal(h.find('team-area-meeting-addressed-agents').textContent,'meeting.panel.noAddressedAgents');
    assert.equal(h.find('chat-panel-input-meeting-record').getAttribute('data-variant'),'recording');
    assert.equal(h.find('team-area-meeting-microphone-btn'),null);
    assert.equal(h.find('chat-panel-input-meeting-record').getAttribute('aria-pressed'),'true');
    assert.equal(h.find('chat-panel-meeting-microphone-state').textContent,'meeting.panel.microphoneStates.recording');
    await h.click('chat-panel-input-meeting-record');
    assert.equal(h.find('chat-panel-input-meeting-record').getAttribute('aria-pressed'),'false');
    assert.equal(h.find('chat-panel-meeting-microphone-state').textContent,'meeting.panel.microphoneStates.ready');
    assert.deepEqual(h.drafts,[]);
    assert.deepEqual(h.messages,['meeting.startMessage','Please review the layout.']);
    assert.equal(h.find('team-area-meeting-transcript').textContent,'Please review the layout.');
    assert.equal(h.find('normal-send'),send);
    const inputs=requests.filter(request=>request.name==='chat.send');
    assert.equal(inputs.length,2);
    assert.deepEqual(inputs[0].params.mentions,[]);
    assert.deepEqual(inputs[1].params.mentions,['leader']);
    assert.ok(inputs.every(input=>input.params.meeting_mode===true));
    await h.click('team-area-meeting-end-btn');
    assert.ok(h.track.stopped);assert.equal(h.capture.at(-1),false);
    assert.ok(requests.some(request=>request.name==='meeting.end'&&request.params.meeting_id==='meeting-one'));
    assert.equal(h.find('team-area-meeting-end-btn'),null);
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
    assert.equal(h.find('team-area-meeting-speech-route').textContent,'meeting.routing.none');
  } finally {await h.close();}
});

test('unconfirmed cleanup preserves End retry and prevents a replacement meeting',async()=>{
 const h=await mount();try {
  await h.click('chat-panel-input-meeting');meetingServer.failEnd=true;
  await h.click('chat-panel-meeting-layer-end');
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'cleanup_failed');
  assert.ok(h.find('chat-panel-meeting-cleanup-error'));
  assert.ok(hasMeetingAudioOwner('s'));
  assert.equal(h.find('chat-panel-input-meeting').disabled,false);
  assert.equal(h.find('team-area-meeting-end-btn').disabled,false);
  await h.updateProps({sessionId:'another-session'});
  assert.equal(h.find('team-area-meeting-end-btn'),null);
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'cleanup_failed');
  meetingServer.failEnd=false;await h.click('chat-panel-meeting-layer-end');
  assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
  assert.equal(requests.filter(r=>r.name==='meeting.start').length,1);
  assert.equal(hasMeetingAudioOwner('s'),false);
 }finally{await h.close();}
});

test('leaving during initial input releases the already reserved startup',async()=>{
 const h=await mount();
 meetingServer.acceptInputs=false;
 await h.click('chat-panel-input-meeting');
 const reserved=requests.find(r=>r.name==='meeting.start');
 assert.ok(reserved);
 assert.equal(h.microphoneCalls(),0);
 await h.close();
 const released=requests.find(r=>r.name==='meeting.release');
 assert.equal(released.params.startup_id,reserved.params.startup_id);
 assert.equal(released.params.session_id,reserved.params.session_id);
});

test('input stays blocked through acceptance and playback preparation, then unlocks',async()=>{
 const h=await mount();let finishReady;
 try {
  meetingServer.acceptInputs=false;
  meetingServer.beforeReady=()=>new Promise(resolve=>{finishReady=resolve;});
  await h.click('chat-panel-input-meeting');
  assert.ok(requests.findIndex(r=>r.name==='meeting.start') < requests.findIndex(r=>r.name==='chat.send'));
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


test('meeting panel shows shared decision and waiting audio with a working remove control', async () => {
 const h=await mount();
 try {
  await h.click('chat-panel-input-meeting');
  await act(async()=>emit('meeting.queue',{session_id:'s',meeting_id:'meeting-one',decision:'Use Canvas',pending:[
   {request_id:'queued-one',member_name:'analyst',wait_seconds:12,speech_text:'New finding'},
  ]}));
  assert.match(h.find('team-area-meeting-decision').textContent,/Use Canvas/);
  assert.match(h.find('team-area-meeting-queue-item').textContent,/New finding/);
  await h.click('team-area-meeting-cancel-queued');
  assert.ok(requests.some(r=>r.name==='meeting.queue.control'&&r.params.operation==='cancel'&&r.params.request_id==='queued-one'));
  assert.equal(h.find('team-area-meeting-queue-item'),null);
  await h.click('team-area-meeting-end-btn');
 } finally {await h.close();}
});


test('meeting speech summaries appear once in the main conversation with the real member identity', async () => {
  const h = await mount();
  try {
    await h.click('chat-panel-input-meeting');
    const speech = {
      session_id: 's', meeting_id: 'meeting-one', request_id: 'short-one',
      lease_token: 'lease-one', source_message_id: 'source-one', speaker_id: 'analyst',
      playback_id: 'playback-one', chunks: ['The layout is ready.'], chunk_count: 1, voice: 'voice-name',
    };
    await act(async () => emit('meeting.speech.ready', {...speech, session_id: 'another'}));
    assert.equal(conversationMessages.length, 0);
    await act(async () => emit('meeting.speech.ready', speech));
    assert.equal(conversationMessages.length, 1);
    assert.equal(conversationMessages[0].content, 'The layout is ready.');
    assert.equal(conversationMessages[0].id, 'team-member-analyst@meeting-source-one');
    assert.equal(conversationMessages[0].sessionId, 's');
    assert.equal(conversationMessages[0].keepExpanded, true);
    await act(async () => emit('meeting.speech.ready', {...speech, playback_id: 'playback-retry'}));
    assert.equal(conversationMessages.length, 1);
    await h.click('team-area-meeting-end-btn');
    await act(async () => emit('meeting.speech.ready', {...speech, source_message_id: 'after-end'}));
    assert.equal(conversationMessages.length, 1);
  } finally { await h.close(); }
});


test('primary composer stop ends the meeting, releases the microphone and exposes cleanup retry', async () => {
  const h = await mount();
  try {
    assert.equal(h.primaryStop(), null);
    await h.click('chat-panel-input-meeting');
    assert.equal(h.primaryStop().sessionId, 's');
    assert.equal(h.primaryStop().disabled, false);
    assert.equal(h.primaryStop().label, 'meeting.task.stop');
    meetingServer.failEnd = true;
    await act(async () => h.primaryStop().onStop());
    assert.equal(h.track.stopped, true);
    assert.equal(h.primaryStop().label, 'meeting.task.retryCleanup');
    assert.equal(h.primaryStop().disabled, false);
    assert.ok(requests.some(request => request.name === 'meeting.end'));
    meetingServer.failEnd = false;
    await act(async () => h.primaryStop().onStop());
    assert.equal(h.primaryStop(), null);
    assert.equal(hasMeetingAudioOwner('s'), false);
  } finally { await h.close(); }
});


test('meeting controls live in a separate collapsible layer and the composer keeps its original action', async () => {
  const h = await mount();
  try {
    await h.click('chat-panel-input-meeting');
    const layer = h.find('chat-panel-meeting-layer');
    const composer = h.find('chat-panel-meeting-controls');
    assert.ok(layer);
    assert.equal(layer.parentElement, document.body);
    assert.equal(composer.querySelectorAll('button').length, 1);
    assert.equal(composer.contains(h.find('chat-panel-input-meeting-record')), false);
    assert.ok(h.find('normal-send'));
    await h.click('chat-panel-meeting-minimize');
    assert.equal(h.find('chat-panel-meeting-layer'), null);
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-expanded'), 'false');
    assert.equal(h.track.stopped, false);
    await h.click('chat-panel-input-meeting');
    assert.ok(h.find('chat-panel-meeting-layer'));
    await h.click('chat-panel-meeting-layer-end');
    assert.equal(h.track.stopped, true);
    assert.equal(h.find('chat-panel-meeting-layer'), null);
  } finally { await h.close(); }
});


test('meeting error details remain visible until dismissed', async()=>{
 const h=await mount();
 try {
  await h.click('chat-panel-input-meeting');
  await act(async()=>emit('chat.error',{session_id:'s',error:'401 invalid API key'}));
  assert.match(h.find('chat-panel-meeting-error-detail').textContent,/401 invalid API key/);
  await h.click('chat-panel-meeting-error-dismiss');
  assert.equal(h.find('chat-panel-meeting-error'),null);
 } finally {await h.close();}
});
