"""Probe getUserMedia / getDisplayMedia inside pywebview (WebView2) like the pywebview desktop app."""
import json, sys, threading, http.server, socketserver
import webview, time, os
from PIL import ImageGrab

def shots():
    for i in range(6):
        time.sleep(4)
        ImageGrab.grab().save(os.path.join('probe-out', f'shot-{i}.png'))

PAGE = b"""<!doctype html><meta charset=utf-8><body><p id=s>probe</p><script>
async function run(){
  const out = {};
  const t = (p) => Promise.race([p, new Promise(r => setTimeout(() => r('HUNG 15s (prompt or no answer)'), 15000))]);
  out.secure = window.isSecureContext; out.hasDisplay = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  out.mic = await t(navigator.mediaDevices.getUserMedia({audio:true}).then(s=>{s.getTracks().forEach(x=>x.stop());return 'GRANTED'}, e=>'REJECT '+e.name+': '+e.message));
  out.display = await t(navigator.mediaDevices.getDisplayMedia({video:true,audio:false}).then(s=>{const r='GRANTED '+s.getVideoTracks()[0].label;s.getTracks().forEach(x=>x.stop());return r}, e=>'REJECT '+e.name+': '+e.message));
  window.pywebview.api.done(JSON.stringify(out));
}
window.addEventListener('pywebviewready', run);
</script>"""

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('content-type', 'text/html'); self.end_headers(); self.wfile.write(PAGE)
    def log_message(self, *a): pass

srv = socketserver.TCPServer(('127.0.0.1', 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

class Api:
    def done(self, payload):
        print('PROBE_RESULT ' + payload, flush=True)
        threading.Timer(1, lambda: win.destroy()).start()

win = webview.create_window('probe', f'http://127.0.0.1:{port}/', js_api=Api())
threading.Timer(60, lambda: (print('PROBE_TIMEOUT', flush=True), win.destroy())).start()
os.makedirs('probe-out', exist_ok=True)
threading.Thread(target=shots, daemon=True).start()
webview.start(gui='edgechromium')
