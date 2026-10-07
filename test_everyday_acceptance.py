import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import test_visual_workspace
from browser_http import browser_owner, create_browser
from browser_runtime import BrowserError
from managed_apps import ManagedApps
from server import Playground, make_server, tmux
from test_visual_workspace_browser import PIXEL, pdf_fixture
from visual_workspace import MAX_FILE, MAX_FILES, MAX_VERSION, VisualError, VisualWorkspace


APP = '''import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
Path("environment.json").write_text(json.dumps({"names": sorted(os.environ), "home": os.environ["HOME"]}))
label = sys.argv[2]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("<h1>" + label + "</h1><p id='storage'></p><script>const before = localStorage.getItem('owner') || 'empty'; localStorage.setItem('owner', '" + label + "'); document.getElementById('storage').textContent = 'before=' + before + '; now=' + localStorage.getItem('owner');</script>").encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
print('ready fixture ' + label, flush=True)
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
'''


class EverydayAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_visual_workspace.VisualTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.app
        self.app.visual = self.fixture.visual

    def test_capture_boundaries_accept_exact_limits_and_reject_excess(self):
        visual = self.app.visual
        data = base64.b64encode(b'x' * MAX_FILE).decode()
        body = self.fixture.body(kind='html', entrypoint='large.html', files=[
            {'path': 'large.html', 'data': data}, {'path': 'second.css', 'data': data}])
        receipt = visual.register(body)
        version = visual.list('@1')['creations'][0]['versions'][0]
        self.assertEqual(version['byte_size'], MAX_VERSION)
        self.assertEqual(len(visual.content(receipt['workspace_id'], receipt['version_id'])[0]), MAX_FILE)
        with self.assertRaises(VisualError):
            visual.register({**body, 'files': body['files'] + [{'path': 'extra.css', 'data': 'eA=='}]})
        with self.assertRaises(VisualError):
            visual.register({**body, 'files': [{'path': 'large.html', 'data': base64.b64encode(b'x' * (MAX_FILE + 1)).decode()}]})
        files = [{'path': f'{number}.html', 'data': 'eA=='} for number in range(MAX_FILES)]
        visual.register(self.fixture.body(kind='html', entrypoint='0.html', files=files))
        with self.assertRaises(VisualError):
            visual.register(self.fixture.body(kind='html', entrypoint='0.html', files=files + [{'path': 'extra.html', 'data': 'eA=='}]))

    def test_two_real_apps_keep_storage_owner_environment_and_saved_history(self):
        self.app.browser = create_browser(self.app.visual)
        self.app.apps = ManagedApps(self.app)
        self.addCleanup(self.clean_apps)
        targets = [self.fixture.target, {'tab': '@2', 'pane': '%2', 'identity': 'exact-b'}]
        owners = [browser_owner(self.app.visual, target) for target in targets]
        records = []
        with patch.dict(os.environ, {'JEV_ACCEPTANCE_SENTINEL': 'synthetic-not-a-secret'}):
            for number, owner in enumerate(owners):
                root = self.fixture.root / f'app-{number}'
                root.mkdir()
                (root / 'app.py').write_text(APP)
                record = self.app.apps.start(owner, root, {'title': f'Acceptance {number}',
                    'command': [sys.executable, 'app.py', '{port}', f'app-{number}'],
                    'build_identity': f'acceptance-build-{number}'})
                records.append(record)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    current = self.app.apps.state(owner=owner)['apps'][0]
                    if current['status'] != 'starting':
                        break
                    time.sleep(.1)
                self.assertEqual(current['status'], 'ready', current)
                self.assertIsNone(current['error'], current)
                self.assertIsNotNone(current['saved_export'])
                environment = json.loads((root / 'environment.json').read_text())
                self.assertLessEqual(set(environment['names']), {'PATH', 'LANG', 'LC_ALL', 'TZ', 'HOME', 'PORT', 'HOST'})
                self.assertNotEqual(environment['home'], os.environ['HOME'])
                pages = self.app.browser.request('pages', owner=owner)['pages']
                page = next(page for page in pages if page['page_id'] == current['page_id'])
                result = self.app.browser.request('action', owner=owner, page_id=page['page_id'], run_id=page['run_id'], account_epoch=page['account_epoch'], action='snapshot')
                self.assertIn(f'before=empty; now=app-{number}', json.dumps(result))
        self.assertNotEqual(records[0]['id'], records[1]['id'])
        with self.assertRaises(VisualError):
            self.app.apps.stop(owners[0], records[1]['id'])
        first = self.app.apps.state(owner=owners[0])['apps'][0]
        initial = first['saved_export']
        saved_before = self.app.visual.content(initial['workspace_id'], initial['version_id'])[0]
        self.assertGreater(len(saved_before), 100)
        self.app.apps.stop(owners[0], records[0]['id'])
        self.assertEqual(self.app.apps.state(owner=owners[0])['apps'][0]['status'], 'stopped')
        second = self.app.apps.state(owner=owners[1])['apps'][0]
        self.assertEqual(second['status'], 'ready')
        self.assertTrue(self.app.apps.alive(self.app.apps.records[records[1]['id']]))
        self.app.apps.stop(owners[1], records[1]['id'])
        restarted = VisualWorkspace(self.app)
        archive = restarted.list_workspace(initial['workspace_id'])
        creation = next(item for item in archive['creations'] if item['id'] == initial['creation_id'])
        self.assertEqual(len(creation['versions']), 2)
        self.assertEqual(restarted.content(initial['workspace_id'], initial['version_id'])[0], saved_before)
        for version in creation['versions']:
            self.assertEqual(version['source']['app']['representation'], 'screenshot')
            self.assertEqual(version['source']['app']['build_identity'], 'acceptance-build-0')
        self.assertIn('saved screenshot', creation['title'])

    def clean_apps(self):
        for record in self.app.apps.records.values():
            if self.app.apps.alive(record) and os.getpgid(record['pid']) == record['pid']:
                os.killpg(record['pid'], signal.SIGTERM)
        self.app.browser.shutdown()


class EverydayDeskUITests(unittest.TestCase):
    def test_real_backend_desktop_phone_readers_draft_and_signin_coordinates(self):
        with tempfile.TemporaryDirectory(prefix='jev-independent-ui-') as directory, ExitStack() as cleanup:
            root = Path(directory)
            socket = str(root / 'tmux.sock')
            tmux(socket, '-f', '/dev/null', 'new-session', '-d', '-s', 'tabs', '-n', 'Desktop report', 'sleep 300')
            cleanup.callback(tmux, socket, 'kill-server')
            tmux(socket, 'new-window', '-d', '-t', 'tabs', '-n', 'Phone browser', 'sleep 300')
            app = Playground(socket, root / 'events.jsonl', independent_selection=True)
            actual_panes = app.panes
            def synthetic_panes(tab):
                panes = actual_panes(tab)
                for pane in panes:
                    pane.update(provider='codex', thread_id='synthetic-' + pane['id'], agent=True, codex=True, identity='acceptance-' + pane['id'])
                return panes
            app.panes = synthetic_panes
            with patch('visual_workspace.agent_process', side_effect=lambda pane: pane['process']):
                server = make_server(app)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = 'http://127.0.0.1:' + str(server.server_port)
                targets = [{'tab': tab['id'], 'pane': tab['panes'][0]['id'], 'identity': tab['panes'][0]['identity']} for tab in app.state()['tabs']]
                def publish(title, kind, name, data, assets=()):
                    return app.visual.register({'target': targets[0], 'title': title, 'kind': kind, 'mode': 'captured', 'entrypoint': name,
                        'files': [{'path': name, 'data': base64.b64encode(data).decode()}, *assets]})
                pdf = publish('Independent two-page report', 'pdf', 'report.pdf', pdf_fixture())
                markdown = publish('Captured Markdown report', 'markdown', 'report.md', b'# Saved garden report\n\nSeven orchids were observed.\n\n![Captured chart](chart.png)\n\n[Source](https://example.test/evidence)\n',
                    [{'path': 'chart.png', 'data': base64.b64encode(PIXEL).decode()}])
                html = publish('Static prototype', 'html', 'index.html', b'<h1>Frozen static prototype</h1><button onclick="this.textContent=\'Clicked locally\'">Try prototype</button>')
                other = publish('Original data download', 'file', 'data.json', b'{"orchids":7}')
                class Site(BaseHTTPRequestHandler):
                    def do_GET(self):
                        data = b'<html><body style="margin:0"><h1>Synthetic account</h1><input aria-label="OTP" style="position:absolute;left:100px;top:100px;width:300px;height:50px" oninput="document.getElementById(\'value\').textContent=this.value"><p id="value" style="position:absolute;top:180px">No input</p></body></html>'
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    def log_message(self, *args):
                        pass
                site = ThreadingHTTPServer(('127.0.0.1', 0), Site)
                threading.Thread(target=site.serve_forever, daemon=True).start()
                owners = [browser_owner(app.visual, target) for target in targets]
                pages = [app.browser.request('open', owner=owner, url=f'http://127.0.0.1:{site.server_port}/') for owner in owners]
                screenshots = Path(__file__).parent / '.run' / 'independent-acceptance'
                screenshots.mkdir(parents=True, exist_ok=True)
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch()
                        desktop = browser.new_page(viewport={'width': 1440, 'height': 1000})
                        phone = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                        errors, controls = [], []
                        for view in (desktop, phone):
                            view.on('pageerror', lambda error: errors.append(str(error)))
                            view.goto(base)
                            view.wait_for_function('currentState !== null')
                        phone.on('request', lambda request: controls.append(request.post_data_json) if request.url.endswith('/api/browser/input') else None)
                        phone.evaluate('(tab) => selectTab(tab)', targets[1]['tab'])
                        phone.wait_for_function('(tab) => currentState.selected === tab', arg=targets[1]['tab'])
                        desktop.locator('#visual-creations-tab').click()
                        desktop.locator('.visual-creation').select_option(pdf['creation_id'])
                        desktop.locator('.pdf-page-view canvas[aria-label="PDF page 1"]').wait_for()
                        desktop.locator('.pdf-next').click()
                        desktop.locator('.pdf-page-view canvas[aria-label="PDF page 2"]').wait_for()
                        desktop.locator('.pdf-zoom').select_option('1.5')
                        desktop.get_by_role('searchbox', name='Find in PDF').fill('First page')
                        desktop.get_by_role('button', name='Find next').click()
                        desktop.wait_for_function('document.querySelector(".reader-status").textContent.startsWith("Page 1:")')
                        desktop.screenshot(path=str(screenshots / 'desktop-pdf.png'), full_page=True)
                        original = desktop.request.get(desktop.locator('.visual-download').evaluate('(link) => link.href'))
                        self.assertEqual(original.body(), pdf_fixture())
                        desktop.locator('.visual-creation').select_option(html['creation_id'])
                        prototype = desktop.frame_locator('.visual-preview iframe')
                        prototype.get_by_role('button', name='Try prototype').click()
                        prototype.get_by_role('button', name='Clicked locally').wait_for()
                        desktop.locator('[data-reader="source"]').click()
                        desktop.locator('.visual-preview pre').wait_for()
                        self.assertIn('<h1>Frozen static prototype</h1>', desktop.locator('.visual-preview pre').inner_text())
                        desktop.locator('[data-reader="read"]').click()
                        desktop.locator('.visual-creation').select_option(other['creation_id'])
                        desktop.locator('.visual-preview').get_by_text('Preview unavailable', exact=True).wait_for()
                        original = desktop.request.get(desktop.locator('.visual-download').evaluate('(link) => link.href'))
                        self.assertEqual(original.body(), b'{"orchids":7}')
                        desktop.locator('.visual-creation').select_option(markdown['creation_id'])
                        reader = desktop.frame_locator('.visual-preview iframe')
                        reader.get_by_role('heading', name='Saved garden report').wait_for()
                        self.assertEqual(reader.locator('img').evaluate('(image) => image.naturalWidth'), 1)
                        self.assertEqual(reader.get_by_role('link', name='Source').get_attribute('target'), '_blank')
                        desktop.locator('#mode-pane').click()
                        desktop.locator('#phone-text').fill('Keep  these exact words about this report.')
                        captured = desktop.evaluate('voice.draft')
                        self.assertEqual(captured['visualReference']['version_id'], markdown['version_id'])
                        self.assertEqual(captured['target']['identity'], targets[0]['identity'])
                        phone.locator('#visual-live-tab').click()
                        phone.locator('.browser-stage canvas:visible').wait_for()
                        phone.locator('.browser-zoom').select_option('2')
                        self.assertEqual(controls, [])
                        phone.locator('.browser-zoom').select_option('fit')
                        phone.locator('.browser-signin').click()
                        phone.locator('.browser-done:visible').wait_for()
                        with self.assertRaises(BrowserError):
                            app.browser.request('action', owner=owners[0], page_id=pages[0]['page_id'], run_id=pages[0]['run_id'], account_epoch=pages[0]['account_epoch'], action='snapshot')
                        canvas = phone.locator('.browser-stage canvas')
                        box = canvas.bounding_box()
                        canvas.tap(position={'x': box['width'] * 150 / 1280, 'y': box['height'] * 125 / 800})
                        phone.wait_for_function('visuals.browser.sequence === 1')
                        phone.locator('.browser-typing').fill('synthetic-otp-654321', force=True)
                        phone.wait_for_function('visuals.browser.sequence === 2')
                        phone.screenshot(path=str(screenshots / 'phone-signin.png'), full_page=True)
                        phone.locator('.browser-done').click()
                        phone.locator('.browser-done').wait_for(state='hidden')
                        fresh = app.browser.request('action', owner=owners[1], page_id=pages[1]['page_id'], run_id=pages[1]['run_id'], account_epoch=pages[1]['account_epoch'], action='snapshot')
                        self.assertIn('synthetic-otp-654321', json.dumps(fresh))
                        self.assertAlmostEqual(controls[0]['args']['x'], 150, delta=2)
                        self.assertAlmostEqual(controls[0]['args']['y'], 125, delta=2)
                        self.assertEqual(desktop.evaluate('currentState.selected'), targets[0]['tab'])
                        self.assertEqual(app.state()['selected'], targets[0]['tab'])
                        self.assertEqual(desktop.evaluate('voice.draft'), captured)
                        desktop.screenshot(path=str(screenshots / 'desktop-markdown.png'), full_page=True)
                        phone.screenshot(path=str(screenshots / 'phone-browser.png'), full_page=True)
                        self.assertFalse(phone.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    app.browser.shutdown()
                    server.shutdown()
                    server.server_close()
                    site.shutdown()
                    site.server_close()


if __name__ == '__main__':
    unittest.main()
