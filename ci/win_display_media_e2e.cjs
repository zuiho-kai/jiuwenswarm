// Windows e2e: run with `electron win_display_media_e2e.cjs`
const { app, BrowserWindow, desktopCapturer } = require('electron');
const fs = require('fs');
const http = require('http');
let baseUrl = '';
const path = require('path');
const { registerDisplayMediaHandler } = require(path.resolve(process.env.PICKER));
const OUT = process.env.OUT_DIR || '.';
const results = {};
const call = `Promise.race([navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'monitor'},audio:false}).then(s=>{const t=s.getVideoTracks()[0];const st=t.getSettings();const r='GRANTED label='+t.label+' state='+t.readyState+' size='+st.width+'x'+st.height;s.getTracks().forEach(x=>x.stop());return r},e=>'REJECT '+e.name+': '+e.message), new Promise(r=>setTimeout(()=>r('HUNG 20s'),20000))])`;
async function page(handler) {
  const w = new BrowserWindow({ show: true, width: 400, height: 300 });
  w.webContents.on('did-fail-load', (_e, code, desc, url) => console.log('E2E_FAIL_LOAD', code, desc, url));
  w.webContents.on('console-message', (_e, _l, msg) => console.log('E2E_CONSOLE', msg));
  if (handler) registerDisplayMediaHandler({ session: w.webContents.session, desktopCapturer, BrowserWindow, getParentWindow: () => w });
  await w.loadURL(baseUrl);
  return w;
}
function onPicker(action) {
  app.once('browser-window-created', (_e, win) => {
    win.webContents.once('did-finish-load', async () => {
      await new Promise(r => setTimeout(r, 1500));
      const n = await win.webContents.executeJavaScript('document.querySelectorAll(".source").length');
      const names = await win.webContents.executeJavaScript('[...document.querySelectorAll(".source .name")].map(x=>x.textContent).join(" | ")');
      const thumbs = await win.webContents.executeJavaScript('document.querySelectorAll(".source img").length');
      results['picker_' + action] = { sources: n, thumbnails: thumbs, names };
      const img = await win.webContents.capturePage();
      fs.writeFileSync(path.join(OUT, 'picker-' + action + '.png'), img.toPNG());
      if (action === 'select') await win.webContents.executeJavaScript('document.querySelector(".source").click()');
      else await win.webContents.executeJavaScript('document.querySelector("button").click()');
    });
  });
}
app.disableHardwareAcceleration();
app.on('render-process-gone', (_e, _w, d) => console.log('E2E_RENDER_GONE ' + JSON.stringify(d)));
app.on('child-process-gone', (_e, d) => console.log('E2E_CHILD_GONE ' + JSON.stringify(d)));
process.on('unhandledRejection', e => { console.log('E2E_ERROR ' + (e && e.stack || e)); app.exit(2); });
app.whenReady().then(async () => {
  const server = http.createServer((_req, res) => { res.setHeader('content-type', 'text/html'); res.end('<p>x</p>'); });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  baseUrl = 'http://127.0.0.1:' + server.address().port + '/';
  let w = await page(false);
  results.no_handler = await w.webContents.executeJavaScript(call, true); w.destroy();
  w = await page(true); onPicker('select');
  results.select = await w.webContents.executeJavaScript(call, true); w.destroy();
  w = await page(true); onPicker('cancel');
  results.cancel = await w.webContents.executeJavaScript(call, true); w.destroy();
  console.log('E2E_RESULTS ' + JSON.stringify(results, null, 2));
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2));
  const ok = results.no_handler.startsWith('REJECT NotSupportedError') && results.select.startsWith('GRANTED') && results.cancel.startsWith('REJECT AbortError');
  console.log(ok ? 'E2E_PASS' : 'E2E_FAIL'); app.exit(ok ? 0 : 1);
});
