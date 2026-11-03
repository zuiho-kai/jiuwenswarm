import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { formatMeetingError, meetingFailure } from '../../../channels/web/frontend/node_modules/.cache/meeting/meetingErrors.mjs';
const locale=JSON.parse(fs.readFileSync(new URL('../../../channels/web/frontend/src/i18n/locales/zh.json',import.meta.url),'utf8'));
const t=key=>key.split('.').reduce((value,part)=>value?.[part],locale) ?? key;

test('microphone permission and missing device receive different recovery instructions',()=>{
 const denied=formatMeetingError(t,'microphoneFailed',meetingFailure('microphoneFailed',Object.assign(new Error('Permission denied'),{name:'NotAllowedError'})));
 const missing=formatMeetingError(t,'microphoneFailed',Object.assign(new Error('No device'),{name:'NotFoundError'}));
 assert.match(denied,/NotAllowedError/);assert.match(denied,/系统麦克风隐私权限/);
 assert.match(missing,/连接并启用麦克风/);assert.doesNotMatch(missing,/隐私权限/);
});
test('ASR upstream code, message and request identity survive with a specific action',()=>{
 const text=formatMeetingError(t,'transcriptionFailed',Object.assign(new Error('401 invalid api key'),{code:'ASR_ERROR',requestId:'request-17'}));
 assert.match(text,/401 invalid api key/);assert.match(text,/ASR_ERROR/);assert.match(text,/request-17/);assert.match(text,/API Key/);
});
test('unknown reasons remain explicitly unknown and tokens are hidden',()=>{
 const text=formatMeetingError(t,'playbackFailed');assert.match(text,/无法确定根因/);assert.doesNotMatch(text,/请检查 Qwen/);
 const raw=formatMeetingError(t,'playbackFailed','401 Bearer secret-value https://example.invalid?api_key=private-value');
 assert.doesNotMatch(raw,/secret-value|private-value/);assert.match(raw,/redacted/);
});
test('autoplay denial is distinct from microphone denial',()=>{
 const text=formatMeetingError(t,'playbackSkipped',Object.assign(new Error('play() disallowed'),{name:'NotAllowedError'}));
 assert.match(text,/允许此站点播放声音/);assert.doesNotMatch(text,/系统麦克风隐私权限/);
});
