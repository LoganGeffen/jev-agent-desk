import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from browser_runtime import BrowserRuntime, BrowserError, ManagedBrowser


class FakeSite(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        if self.path == '/download':
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Disposition', 'attachment; filename=report.txt')
            self.end_headers()
            self.wfile.write(b'completed disposable report')
            return
        self.send_header('Content-Type', 'text/html')
        if self.path == '/login':
            self.send_header('Set-Cookie', 'account=disposable; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax')
        self.end_headers()
        account = 'signed in' if 'account=disposable' in self.headers.get('Cookie', '') else 'signed out'
        color = '#ffcccc' if self.path.startswith('/a') else '#ccccff'
        self.wfile.write(f'''<html style="background:{color}"><title>{self.path}</title><body><h1>{self.path} {account}</h1>
        <a id="download" href="/download">Download</a><input id="input"><button id="popup" onclick="window.open('/popup', '_blank')">Popup</button>
        <button id="change" onclick="document.querySelector('h1').textContent='changed {self.path}'">Change</button><script>setTimeout(() => {{const b=document.createElement('button');b.id='delayed';b.textContent='Delayed';b.onclick=()=>document.querySelector('h1').textContent='wrong delayed effect';document.body.append(b)}},400)</script></body></html>'''.encode())


def owner(name):
    return {'workspace_id': 'workspace-' + name, 'target': {'tab': '@' + name, 'pane': '%' + name, 'identity': name},
            'provider': 'codex', 'thread_id': 'thread-' + name, 'process': '123:456', 'agent_process': '123:456'}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.site = ThreadingHTTPServer(('127.0.0.1', 0), FakeSite)
        self.thread = threading.Thread(target=self.site.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.site.server_port)
        self.runtime = await BrowserRuntime(Path(self.temporary.name) / 'browser').start()

    async def asyncTearDown(self):
        await self.runtime.close()
        await asyncio.to_thread(self.site.shutdown)
        self.site.server_close()
        self.temporary.cleanup()

    async def action(self, who, page, action, **args):
        return await self.runtime.request('action', owner=owner(who), page_id=page['page_id'], run_id=page['run_id'],
                                          account_epoch=page['account_epoch'], action=action, args=args)

    async def test_exact_pages_shared_login_popup_closure_restart(self):
        a = await self.runtime.request('open', owner=owner('a'), url=self.url + '/login')
        await self.action('a', a, 'navigate', url=self.url + '/a')
        b = await self.runtime.request('open', owner=owner('b'), url=self.url + '/b')
        self.assertIn('signed in', (await self.action('b', b, 'snapshot'))['result']['text'])
        frames = []
        for who, page in [('a', a), ('b', b)]:
            frame = await self.runtime.request('frame', owner=owner(who), page_id=page['page_id'], run_id=page['run_id'])
            frames.append(hashlib.sha256(frame['data'].encode()).hexdigest())
        self.assertNotEqual(*frames)
        with self.assertRaises(BrowserError):
            await self.action('b', a, 'click', selector='#change')
        with self.assertRaises(BrowserError):
            await self.runtime.request('frame', owner=owner('b'), page_id=a['page_id'], run_id=a['run_id'])
        await self.action('a', a, 'click', selector='#change')
        self.assertIn('/b signed in', (await self.action('b', b, 'snapshot'))['result']['text'])
        await self.action('a', a, 'click', selector='#popup')
        await asyncio.sleep(.2)
        pages = (await self.runtime.request('pages', owner=owner('a')))['pages']
        popup = next(p for p in pages if p.get('parent_page_id') == a['page_id'])
        self.assertEqual(len((await self.runtime.request('pages', owner=owner('b')))['pages']), 1)
        with self.assertRaises(BrowserError):
            await self.action('b', popup, 'snapshot')
        await self.action('a', a, 'close')
        with self.assertRaises(BrowserError):
            await self.action('a', a, 'snapshot')
        self.assertIn('/b signed in', (await self.action('b', b, 'snapshot'))['result']['text'])
        await self.runtime.close()
        self.runtime = await BrowserRuntime(Path(self.temporary.name) / 'browser').start()
        new = await self.runtime.request('open', owner=owner('b'), url=self.url + '/b')
        self.assertNotEqual(new['run_id'], b['run_id'])
        self.assertIn('signed in', (await self.action('b', new, 'snapshot'))['result']['text'])
        with self.assertRaises(BrowserError):
            await self.action('b', b, 'click', selector='#change')
        with self.assertRaises(BrowserError):
            await self.runtime.request('frame', owner=owner('b'), page_id=b['page_id'], run_id=b['run_id'])

    async def test_profile_lease_blocks_agents_and_stale_input(self):
        a = await self.runtime.request('open', owner=owner('a'), url=self.url + '/a')
        b = await self.runtime.request('open', owner=owner('b'), url=self.url + '/b')
        c = await self.runtime.request('open', owner=owner('c'), profile_id='separate', url=self.url + '/c')
        lease = await self.runtime.request('lease', owner=owner('a'), page_id=a['page_id'], run_id=a['run_id'], client_id='phone')
        with self.assertRaises(BrowserError):
            await self.runtime.request('pages', owner=owner('b'))
        with self.assertRaises(BrowserError):
            await self.runtime.request('lease', owner=owner('b'), page_id=b['page_id'], run_id=b['run_id'], client_id='second-phone')
        for action in ('snapshot', 'screenshot', 'click'):
            with self.assertRaises(BrowserError):
                await self.action('b', b, action, selector='#change')
        self.assertIn('/c', (await self.action('c', c, 'snapshot'))['result']['text'])
        human = dict(owner=owner('a'), page_id=a['page_id'], run_id=a['run_id'], client_id='phone', lease_id=lease['lease_id'])
        await self.runtime.request('input', **human, sequence=1, action='click', args={'x': 20, 'y': 90})
        with self.assertRaises(BrowserError):
            await self.runtime.request('input', **human, sequence=1, action='text', args={'text': 'must not replay'})
        await self.runtime.request('disconnect', **human)
        with self.assertRaises(BrowserError):
            await self.action('b', b, 'snapshot')
        with self.assertRaises(BrowserError):
            await self.runtime.request('input', **human, sequence=2, action='text', args={'text': 'stale'})
        lease = await self.runtime.request('lease', owner=owner('a'), page_id=a['page_id'], run_id=a['run_id'], client_id='desktop')
        await self.runtime.request('done', owner=owner('a'), page_id=a['page_id'], run_id=a['run_id'], client_id='desktop', lease_id=lease['lease_id'])
        with self.assertRaises(BrowserError):
            await self.action('b', b, 'click', selector='#change')
        fresh = await self.action('b', b, 'snapshot')
        self.assertEqual(fresh['account_epoch'], 1)
        await self.action('b', fresh, 'click', selector='#change')
        with self.assertRaises(BrowserError) as error:
            await self.runtime.request('open', owner=owner('d'), profile_id='third', url=self.url)
        self.assertEqual(error.exception.status, 429)


    async def test_replaced_owner_rejected_after_wait_before_effect(self):
        a = await self.runtime.request('open', owner=owner('a'), url=self.url + '/a')
        valid = True
        async def guard(value):
            return valid
        profile = self.runtime.profiles['desk']
        await profile['lock'].acquire()
        request = asyncio.create_task(self.runtime.request('action', _guard=guard, owner=owner('a'), page_id=a['page_id'],
            run_id=a['run_id'], account_epoch=0, action='click', args={'selector': '#change'}))
        await asyncio.sleep(.05)
        valid = False
        profile['lock'].release()
        with self.assertRaises(BrowserError):
            await request
        self.assertIn('/a signed out', (await self.action('a', a, 'snapshot'))['result']['text'])
        valid = True
        request = asyncio.create_task(self.runtime.request('action', _guard=guard, owner=owner('a'), page_id=a['page_id'],
            run_id=a['run_id'], account_epoch=0, action='click', args={'selector': '#delayed'}))
        await asyncio.sleep(.05)
        valid = False
        with self.assertRaises(BrowserError):
            await request
        self.assertNotIn('wrong delayed effect', (await self.action('a', a, 'snapshot'))['result']['text'])

    async def test_popup_human_input_expiry_and_completed_download_ack(self):
        a = await self.runtime.request('open', owner=owner('a'), url=self.url + '/a')
        await self.action('a', a, 'click', selector='#download')
        for _ in range(20):
            downloads = (await self.runtime.request('downloads', owner=owner('a')))['downloads']
            if downloads:
                break
            await asyncio.sleep(.05)
        self.assertEqual(downloads[0]['name'], 'report.txt')
        self.assertEqual((await self.runtime.request('downloads', owner=owner('b')))['downloads'], [])
        await self.runtime.request('ack_download', owner=owner('a'), download_id=downloads[0]['download_id'])
        self.assertEqual((await self.runtime.request('downloads', owner=owner('a')))['downloads'], [])
        lease = await self.runtime.request('lease', owner=owner('a'), page_id=a['page_id'], run_id=a['run_id'], client_id='phone')
        human = dict(owner=owner('a'), page_id=a['page_id'], run_id=a['run_id'], client_id='phone', lease_id=lease['lease_id'])
        page = self.runtime.pages[a['page_id']]['page']
        box = await page.locator('#popup').bounding_box()
        await self.runtime.request('input', **human, sequence=1, action='click', args={'x': box['x']+5, 'y': box['y']+5})
        await asyncio.sleep(.1)
        popup = next(p for p in (await self.runtime.request('view_pages', owner=owner('a')))['pages'] if p['parent_page_id'])
        popup_human = {**human, 'page_id': popup['page_id']}
        await self.runtime.request('input', **popup_human, sequence=2, action='key', args={'key': 'Tab'})
        self.runtime.profiles['desk']['lease']['until'] = time.monotonic() - 1
        with self.assertRaises(BrowserError):
            await self.runtime.request('heartbeat', **human)
        self.assertEqual(self.runtime.profiles['desk']['mode'], 'waiting')
        with self.assertRaises(BrowserError):
            await self.action('a', a, 'snapshot')

    async def test_same_conversation_rebind_fences_old_handles_and_new_popups(self):
        a = await self.runtime.request('open', owner=owner('a'), url=self.url + '/a')
        resumed = {**owner('a'), 'process': 'new:1', 'agent_process': 'new:1',
                   'target': {**owner('a')['target'], 'identity': 'new:identity'}}
        rebound = (await self.runtime.request('rebind', owner=resumed, previous_owner=owner('a')))['pages'][0]
        self.assertNotEqual(rebound['page_id'], a['page_id'])
        with self.assertRaises(BrowserError):
            await self.action('a', a, 'snapshot')
        await self.runtime.request('action', owner=resumed, page_id=rebound['page_id'], run_id=rebound['run_id'],
                                   account_epoch=0, action='click', args={'selector': '#popup'})
        await asyncio.sleep(.1)
        self.assertEqual(len((await self.runtime.request('pages', owner=resumed))['pages']), 2)
        self.assertEqual((await self.runtime.request('pages', owner=owner('a')))['pages'], [])


class ServiceTests(unittest.TestCase):
    def test_http_client_restart_recovers_same_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            first = ManagedBrowser(directory)
            try:
                page = first.request('open', owner=owner('a'))
                second = ManagedBrowser(directory)
                pages = second.request('pages', owner=owner('a'))['pages']
                self.assertEqual(pages[0]['page_id'], page['page_id'])
                self.assertEqual(pages[0]['run_id'], page['run_id'])
                second.request('action', owner=owner('a'), page_id=page['page_id'], run_id=page['run_id'], account_epoch=0, action='snapshot')
            finally:
                first.shutdown()
                for _ in range(100):
                    if not first.record_path.exists():
                        break
                    time.sleep(.05)


if __name__ == '__main__':
    unittest.main()
