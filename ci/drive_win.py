"""Drive the frozen WorkSwarm frontend in Chrome (fake mic) and print the full-duplex start telemetry."""
import asyncio, re, sys
from playwright.async_api import async_playwright
URL = sys.argv[1]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel='chrome', args=['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required'])
        ctx = await b.new_context(viewport={'width': 1280, 'height': 800}, permissions=['microphone', 'camera'])
        pg = await ctx.new_page()
        logs = []
        pg.on('console', lambda m: logs.append(f'CONSOLE[{m.type}] {m.text}'[:300]) if m.type in ('error', 'warning') else None)
        pg.on('pageerror', lambda e: logs.append('PAGEERROR ' + str(e)[:300]))
        def onws(ws):
            logs.append('WS ' + ws.url)
            def sent(f):
                if isinstance(f, str) and 'video.realtime.telemetry' in f:
                    ev = re.search(r'"event"\s*:\s*"([^"]+)"', f); msg = re.search(r'"(?:message|error|reason)"\s*:\s*"([^"]{0,200})', f)
                    logs.append('TELEMETRY ' + (ev[1] if ev else '?') + (' :: ' + msg[1] if msg else ''))
            ws.on('framesent', sent)
            ws.on('framereceived', lambda f: logs.append('RECV ' + f[:300]) if isinstance(f, str) and ws.url.endswith('qwen-omni') else None)
            ws.on('close', lambda _w: logs.append('WSCLOSE ' + ws.url))
        pg.on('websocket', onws)
        await pg.goto(URL); await pg.wait_for_timeout(10000)
        n = await pg.locator('[data-testid="chat-panel-input-full-duplex"]').count()
        print('duplex button count:', n)
        await pg.evaluate("document.querySelector('[data-testid=chat-panel-input-full-duplex]').click()")
        await pg.wait_for_timeout(25000)
        await pg.screenshot(path='frozen-out/after-click.png')
        ctl = pg.locator('[data-testid="chat-panel-duplex-controls"]')
        print('orb variant after 25s:', await ctl.get_attribute('data-variant') if await ctl.count() else 'none')
        print('toasts:', await pg.locator('[role="alert"], [class*="toast"]').all_inner_texts())
        print('\n'.join(l for l in logs if not l.startswith('CONSOLE[warning]')))
        await b.close()
asyncio.run(main())
