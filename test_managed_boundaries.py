import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app_runtime_identity import listener_identity
from browser_runtime import BrowserRuntime, BrowserError
from server import make_server
from test_browser_runtime import owner
from test_conversation_browser import fixture_state


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = await BrowserRuntime(self.root / 'browser').start()
        self.servers = []
        self.children = []

    async def asyncTearDown(self):
        await self.runtime.close()
        for server in self.servers:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        for child in self.children:
            child.terminate()
            child.wait(timeout=5)
        self.temporary.cleanup()

    def http(self, handler, port=0):
        server = ThreadingHTTPServer(('127.0.0.1', port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    async def action(self, receipt, action, **args):
        return await self.runtime.request('action', owner=owner('a'), **{key: receipt[key] for key in ('page_id', 'run_id', 'account_epoch')}, action=action, args=args)

    async def test_managed_requests_cannot_embed_navigate_or_spoof_access_to_desk(self):
        inputs, state = [], fixture_state()
        app = SimpleNamespace(log=self.root / 'events.jsonl', state=lambda *args: state)
        app.terminal_input = lambda tab, **kwargs: inputs.append(tab) or state
        desk = make_server(app, public_origin='https://desk.example.test')
        threading.Thread(target=desk.serve_forever, daemon=True).start()
        self.servers.append(desk)
        origin = f'http://127.0.0.1:{desk.server_port}'
        class Generated(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(f'<h1>Owned app</h1><iframe src="{origin}/"></iframe>'.encode())
        generated = self.http(Generated)
        app_page = await self.runtime.request('open', owner=owner('a'), url=f'http://127.0.0.1:{generated.server_port}')
        page = self.runtime.pages[app_page['page_id']]['page']
        with self.assertRaises(Exception):
            await self.action(app_page, 'click', selector='iframe >> internal:control=enter-frame >> button.tab[data-tab-id="@b"]')
        spoof = await page.evaluate('''async origin => { try { const response=await fetch(origin+'/api/input',{method:'POST',headers:{'Content-Type':'application/json','X-Jev-Managed-Browser':'0'},body:JSON.stringify({tab:'@b',pane:'%b',identity:'b',key:'X'})});return response.status } catch {return 'blocked'} }''', origin)
        self.assertIn(spoof, (403, 'blocked'))
        worker = await page.evaluate("navigator.serviceWorker.register('/worker.js').then(r => Boolean(r), () => false)")
        self.assertFalse(worker)
        self.assertIsNone(await page.evaluate('navigator.serviceWorker.controller'))
        self.assertEqual(page.context.service_workers, [])
        for host in ('127.0.0.1', 'localhost'):
            direct = await self.runtime.request('open', owner=owner('a'), url=f'http://{host}:{desk.server_port}/')
            snapshot = await self.action(direct, 'snapshot')
            self.assertIn('Managed browsers cannot access Desk controls', snapshot['result']['text'])
        with self.assertRaises(HTTPError) as denied:
            await asyncio.to_thread(urlopen, Request(origin + '/api/input', data=b'{}', headers={'Host': 'desk.example.test', 'Origin': 'https://desk.example.test', 'X-Jev-Managed-Browser': '1', 'Content-Type': 'application/json'}))
        self.assertEqual(denied.exception.code, 403)
        self.assertEqual(inputs, [])
        self.assertIn('Owned app', (await self.action(app_page, 'snapshot'))['result']['text'])

    async def app_listener(self, name):
        root = self.root / name
        root.mkdir()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        code = '''import os,sys,time,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b"<h1>Owned app</h1><script>window.effects=0;setTimeout(()=>{let b=document.createElement('button');b.id='late';b.textContent='late';b.onclick=()=>window.effects++;document.body.append(b)},1600)</script>")
s=HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler)
threading.Thread(target=s.serve_forever,daemon=True).start()
Path('ready').touch()
while not Path('release').exists():time.sleep(.01)
s.shutdown();s.server_close();Path('released').touch()
time.sleep(60)
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(port)], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        for _ in range(100):
            if (root / 'ready').exists():
                break
            await asyncio.sleep(.02)
        binding = listener_identity(child.pid, port)
        self.assertIsNotNone(binding)
        await self.runtime.request('bind_app', owner=owner('a'), profile_id='app-' + name, binding=binding)
        receipt = await self.runtime.request('open', owner=owner('a'), profile_id='app-' + name, url=f'http://127.0.0.1:{port}/')
        return root, port, child, receipt

    async def replace_listener(self, root, port):
        (root / 'release').touch()
        for _ in range(100):
            if (root / 'released').exists():
                break
            await asyncio.sleep(.02)
        class Foreign(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'FOREIGN PROCESS CONTENT')
        return self.http(Foreign, port)

    async def test_app_takeover_blocks_delayed_effect_frames_and_background_requests(self):
        root, port, child, receipt = await self.app_listener('delayed')
        unrelated = await self.runtime.request('open', owner=owner('a'), url='about:blank')
        delayed = asyncio.create_task(self.action(receipt, 'click', selector='#late'))
        await asyncio.sleep(.1)
        await self.replace_listener(root, port)
        self.assertIsNone(child.poll())
        with self.assertRaisesRegex(BrowserError, 'listener ownership'):
            await delayed
        page = self.runtime.pages[receipt['page_id']]['page']
        self.assertEqual(await page.evaluate('window.effects'), 0)
        blocked_fetch = await page.evaluate("fetch('/changed').then(r=>r.text(),()=> 'blocked')")
        self.assertEqual(blocked_fetch, 'blocked')
        with self.assertRaisesRegex(BrowserError, 'listener ownership'):
            await self.runtime.request('frame', owner=owner('a'), page_id=receipt['page_id'], run_id=receipt['run_id'])
        with self.assertRaisesRegex(BrowserError, 'listener ownership'):
            await self.action(receipt, 'reload')
        pages = (await self.runtime.request('pages', owner=owner('a')))['pages']
        self.assertEqual(next(page for page in pages if page['page_id'] == receipt['page_id'])['status'], 'stale')
        await self.action(unrelated, 'snapshot')

    async def test_app_takeover_during_screenshot_discards_completed_pixels(self):
        root, port, _, receipt = await self.app_listener('pixels')
        page = self.runtime.pages[receipt['page_id']]['page']
        original = page.screenshot
        async def screenshot(**kwargs):
            result = await original(**kwargs)
            await self.replace_listener(root, port)
            return result
        with patch.object(page, 'screenshot', side_effect=screenshot):
            with self.assertRaisesRegex(BrowserError, 'listener ownership'):
                await self.runtime.request('frame', owner=owner('a'), page_id=receipt['page_id'], run_id=receipt['run_id'])
