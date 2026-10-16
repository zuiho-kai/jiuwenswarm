// Windows e2e: `electron win_display_media_e2e.cjs` (PICKER=path to display_media_picker.cjs, OUT_DIR=artifacts)
const { app, BrowserWindow, desktopCapturer } = require('electron');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { registerDisplayMediaHandler } = require(path.resolve(process.env.PICKER));
const OUT = process.env.OUT_DIR || '.';
const results = {};
const call = `Promise.race([navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'monitor'},audio:false}).then(s=>{const t=s.getVideoTracks()[0];const st=t.getSettings();const r='GRANTED label='+t.label+' state='+t.readyState+' size='+st.width+'x'+st.height;s.getTracks().forEach(x=>x.stop());return r},e=>'REJECT '+e.name+': '+e.message), new Promise(r=>setTimeout(()=>r('HUNG 20s'),20000))])`;
process.on('unhandledRejection', e => { console.log('E2E_ERROR ' + (e && e.stack || e)); app.exit(2); });
function onPicker(action) {
  app.once('browser-window-created', (_e, win) => {
    win.webContents.once('did-finish-load', async () => {
      await new Promise(r => setTimeout(r, 1500));
      results['picker_' + action] = await win.webContents.executeJavaScript('({sources: document.querySelectorAll(".source").length, thumbnails: document.querySelectorAll(".source img").length, names: [...document.querySelectorAll(".source .name")].map(x=>x.textContent).join(" | ")})');
      fs.writeFileSync(path.join(OUT, 'picker-' + action + '.png'), (await win.webContents.capturePage()).toPNG());
      await win.webContents.executeJavaScript(action === 'select' ? 'document.querySelector(".source").click()' : 'document.querySelector("button").click()');
    });
  });
}
app.whenReady().then(async () => {
  const server = http.createServer((_req, res) => { res.setHeader('content-type', 'text/html'); res.end('<p>x</p>'); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const w = new BrowserWindow({ show: true, width: 400, height: 300 });
  await w.loadURL('http://127.0.0.1:' + server.address().port + '/');
  console.log('STEP no_handler');
  results.no_handler = await w.webContents.executeJavaScript(call, true);
  registerDisplayMediaHandler({ session: w.webContents.session, desktopCapturer, BrowserWindow, getParentWindow: () => w });
  console.log('STEP select'); onPicker('select');
  results.select = await w.webContents.executeJavaScript(call, true);
  console.log('STEP cancel'); onPicker('cancel');
  results.cancel = await w.webContents.executeJavaScript(call, true);
  console.log('E2E_RESULTS ' + JSON.stringify(results, null, 2));
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2));
  const ok = results.no_handler.startsWith('REJECT NotSupportedError') && results.select.startsWith('GRANTED') && results.cancel.startsWith('REJECT AbortError');
  console.log(ok ? 'E2E_PASS' : 'E2E_FAIL'); app.exit(ok ? 0 : 1);
});
