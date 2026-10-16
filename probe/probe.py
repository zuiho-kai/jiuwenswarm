import os, sys, threading, time, webview
case = sys.argv[1]
print(f"=== CASE {case} | WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS={os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS')!r} | pywebview={getattr(webview,'__version__','?')}", flush=True)
done = threading.Event()
class Api:
    def report(self, s):
        print(f"[{case}] {s}", flush=True)
        if '"stage":"result"' in s: done.set()
win = webview.create_window('probe', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'), js_api=Api())
def watchdog():
    if not done.wait(15): print(f"[{case}] TIMEOUT 15s: getDisplayMedia did not settle", flush=True)
    time.sleep(1); win.destroy()
webview.start(watchdog, gui='edgechromium', http_server=True, debug=False)
print(f"=== END {case}", flush=True)
