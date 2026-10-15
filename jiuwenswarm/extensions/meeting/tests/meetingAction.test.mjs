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
    export {requests} from 'meeting-test-transport';
  `, resolveDir: frontend },
  bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', packages: 'external',
  outfile: output, loader: { '.css': 'empty' },
  plugins: [{ name: 'meeting-action-test-host', setup(build) {
    build.onResolve({filter:/react-i18next$/},()=>({path:'i18n',namespace:'meeting-test'}));
    build.onResolve({filter:/stores\/sessionStore$/},()=>({path:'sessions',namespace:'meeting-test'}));
    build.onResolve({filter:/services\/webClient$|^meeting-test-transport$/},()=>({path:'transport',namespace:'meeting-test'}));
    build.onResolve({filter:/Toast\/toastStore$/},()=>({path:'toast',namespace:'meeting-test'}));
    build.onLoad({filter:/.*/,namespace:'meeting-test'},({path})=>({loader:'js',contents: {
      i18n: `export const useTranslation=()=>({t:key=>key,i18n:{language:'en'}});`,
      sessions: `const runtime={mode:'team',teamLeaderMemberIds:['leader'],teamMembers:[]};const state={runtimes:{s:runtime},getRuntime:()=>runtime};export const useSessionStore=fn=>fn(state);useSessionStore.getState=()=>state;`,
      transport: `export const requests=[];export const webClient={getState:()=> 'ready',on:()=>()=>{},onStateChange:()=>()=>{},request:async(name,params)=>{requests.push({name,params});return name==='config.get'?{team_meeting_enabled:true}:name==='video.transcribe'?{transcript:'Please review the layout.'}:{};}};`,
      toast: `export const toast={open:()=>{}};`,
    }[path]}));
  }}],
});
const {TaskMeetingAction,TaskMeetingRuntime,setTeamMeetingEnabled,requests}=require(output);

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
  setTeamMeetingEnabled(false);requests.length=0;
  const drafts=[],capture=[];
  const root=createRoot(document.getElementById('root'));
  const props={fallback:React.createElement('button',{'data-testid':'normal-send'},'Send'),eligible:false,teamEligible:true,sessionId:'s',ensureSession:async()=> 's',onTranscript:text=>drafts.push(text),onMicrophoneActiveChange:value=>capture.push(value),labels:{start:'',starting:'',stop:''},...extra};
  const render=(key)=>root.render(React.createElement(React.Fragment,null,React.createElement(TaskMeetingRuntime,{sessionId:'s',key:'runtime'}),React.createElement(TaskMeetingAction,{...props,key})));
  await act(async()=>render('welcome')); 
  return {dom,track,drafts,capture,root,microphoneCalls:()=>microphoneCalls,
    rerenderComposer:async()=>{await act(async()=>render('conversation'));},
    updateProps:async(update)=>{Object.assign(props,update);await act(async()=>render('welcome'));},
    find:id=>document.querySelector(`[data-testid="${id}"]`),
    click:async id=>{await act(async()=>{document.querySelector(`[data-testid="${id}"]`).click();});},
    close:async()=>{await act(async()=>root.unmount());dom.window.close();},
  };
}

test('meeting capture fills the normal editor and preserves its Send button',async()=>{
  const h=await mount();
  try {
    const send=h.find('normal-send');
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('aria-pressed'),'true');
    assert.equal(h.find('chat-panel-input-meeting-record').getAttribute('data-variant'),'recording');
    await h.click('chat-panel-input-meeting-record');
    assert.deepEqual(h.drafts,['Please review the layout.']);
    assert.equal(h.find('normal-send'),send);
    assert.ok(!requests.some(request=>request.name==='chat.send'));
    await h.click('chat-panel-input-meeting');
    assert.ok(h.track.stopped);assert.equal(h.capture.at(-1),false);
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

test('cancelling pending session creation never starts microphone capture',async()=>{
  let ready;
  const h=await mount({ensureSession:()=>new Promise(resolve=>{ready=resolve;})});
  try {
    await h.click('chat-panel-input-meeting');
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'starting');
    await h.click('chat-panel-input-meeting');
    await act(async()=>ready('s'));
    assert.equal(h.microphoneCalls(),0);
    assert.equal(h.find('chat-panel-input-meeting').getAttribute('data-variant'),'idle');
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
    assert.deepEqual(h.drafts,['Please review the layout.']);
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
