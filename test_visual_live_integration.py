import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import shlex
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
import uuid

from playwright.sync_api import sync_playwright
from server import tmux
from test_visual_integration import VisualHttpFixture


@unittest.skipUnless(os.environ.get('JEV_TEST_AGENT_BROWSER') == '1',
                     'Opt in to the disposable installed agent-browser integration with JEV_TEST_AGENT_BROWSER=1')
class InstalledBrowserIntegrationTests(VisualHttpFixture):
    def test_installed_named_browser_through_passive_desk_relay_and_listener_replacement(self):
        runtime = Path(tempfile.mkdtemp(prefix='jev-live-'))
        self.addCleanup(shutil.rmtree, runtime)
        runtime.chmod(0o700)
        config = runtime / 'config.json'
        config.write_text('{}')
        session = 'jev-desk-test-' + uuid.uuid4().hex[:10]
        environment = {key: value for key, value in os.environ.items()
                       if key in {'HOME', 'PATH', 'USER', 'LANG', 'LD_LIBRARY_PATH'}}
        environment.update(AGENT_BROWSER_SOCKET_DIR=str(runtime), AGENT_BROWSER_IDLE_TIMEOUT_MS='120000')
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
        command_prefix = ['agent-browser', '--config', str(config), '--session', session,
                          '--executable-path', executable, '--json']
        def command(*args):
            result = subprocess.run(command_prefix + list(args), env=environment, text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = json.loads(result.stdout)
            self.assertTrue(result['success'], result.get('error'))
            return result['data']
        class SyntheticPage(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = b'<html><title>Desk stream fixture</title><body style="background:#900;color:white;font:48px sans-serif">LIVE ONE</body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        page_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SyntheticPage)
        page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
        page_thread.start()
        self.addCleanup(page_thread.join, 2)
        self.addCleanup(page_server.server_close)
        self.addCleanup(page_server.shutdown)
        self.addCleanup(command, 'close')
        command('open', f'http://127.0.0.1:{page_server.server_port}/')
        pid = int((runtime / (session + '.pid')).read_text())
        port = int((runtime / (session + '.stream')).read_text())
        identity = {key: self.targets['Dog Data'][key] for key in ('tab', 'pane', 'identity')}
        attachment = self.post('/api/visual/attach', {'target': identity, 'name': 'Disposable real browser',
                                                    'session': session, 'port': port, 'pid': pid})
        self.assertIsNone(self.workspace('Controller')['live'])
        state_expression = 'JSON.stringify({url:location.href,width:innerWidth,height:innerHeight,text:document.body.textContent})'
        before = json.loads(command('eval', state_expression)['result'])
        self.app.focus(identity['tab'])
        with sync_playwright() as playwright:
            viewer_browser = playwright.chromium.launch()
            try:
                page = viewer_browser.new_page(viewport={'width': 1440, 'height': 900})
                page.goto(self.url)
                page.locator('#visual-live-tab').click()
                page.locator('.visual-live-stage canvas').wait_for(state='visible', timeout=8000)
                first_canvas = page.locator('.visual-live-stage canvas').evaluate('(canvas) => canvas.toDataURL()')
                command('eval', "document.body.style.background='#090';document.body.textContent='LIVE UI CHANGE'")
                page.wait_for_function("image => document.querySelector('.visual-live-stage canvas').toDataURL() !== image", arg=first_canvas)
                page.evaluate('tab => selectTab(tab)', self.targets['Controller']['tab'])
                page.wait_for_function("document.querySelector('.visual-live-stage canvas').hidden")
                self.assertIsNone(self.workspace('Controller')['live'])
                deadline = time.monotonic() + 4
                while self.app.visual.streaming and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                page.evaluate('tab => selectTab(tab)', identity['tab'])
                page.locator('.visual-live-stage canvas').wait_for(state='visible', timeout=8000)
                page.evaluate('visuals.stream.onerror()')
                page.locator('.visual-retry').wait_for(state='visible')
                deadline = time.monotonic() + 4
                while self.app.visual.streaming and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                page.locator('.visual-retry').click()
                page.locator('.visual-live-stage canvas').wait_for(state='visible', timeout=8000)
                output = Path(__file__).parent / '.run/visual-integration'
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'live-real-browser.png'), full_page=True)
            finally:
                viewer_browser.close()
        deadline = time.monotonic() + 4
        while self.app.visual.streaming and time.monotonic() < deadline:
            time.sleep(.1)
        command('eval', "document.body.style.background='#900';document.body.textContent='LIVE ONE'")
        messages = queue.Queue()
        stop = threading.Event()
        stream = attachment['live']['stream_url']
        def observe():
            try:
                with urlopen(self.url + stream, timeout=5) as response:
                    event = None
                    while not stop.is_set():
                        line = response.readline().decode()
                        if not line:
                            break
                        if line.startswith('event: '):
                            event = line[7:].strip()
                        elif line.startswith('data: '):
                            messages.put((event, json.loads(line[6:])))
            except Exception as error:
                messages.put(('error', str(error)))
        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        events = []
        def next_frame():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                event, data = messages.get(timeout=5)
                events.append(event)
                self.assertIn(event, ('status', 'frame'), data)
                if event == 'frame':
                    self.assertEqual(data['generation'], attachment['live']['generation'])
                    self.assertEqual(data['mime'], 'image/jpeg')
                    self.assertGreater(data['width'], 0)
                    self.assertGreater(data['height'], 0)
                    self.assertTrue(base64.b64decode(data['data']).startswith(b'\xff\xd8\xff'))
                    return data
            self.fail('Desk relay did not receive an actual browser frame')
        try:
            first = next_frame()
            during = json.loads(command('eval', state_expression)['result'])
            self.assertEqual(before, during)
            command('eval', "document.body.style.background='#009';document.body.textContent='LIVE TWO'")
            second = next_frame()
            while second['data'] == first['data']:
                second = next_frame()
            self.assertGreater(second['seq'], first['seq'])
        finally:
            stop.set()
            thread.join(7)
        self.assertFalse(thread.is_alive(), 'Viewer disconnect must finish without controlling the browser')
        after = json.loads(command('eval', state_expression)['result'])
        self.assertEqual({key: before[key] for key in ('url', 'width', 'height')},
                         {key: after[key] for key in ('url', 'width', 'height')})
        self.assertEqual(after['text'], 'LIVE TWO')
        self.post('/api/visual/detach', {'tab': identity['tab'], 'attachment_id': attachment['live']['id']})
        self.assertEqual(json.loads(command('eval', state_expression)['result']), after)
        attachment = self.post('/api/visual/attach', {'target': identity, 'name': 'Replacement test',
                                                    'session': session, 'port': port, 'pid': pid})
        command('stream', 'disable')
        command('stream', 'enable', '--port', str(port))
        self.assertEqual(int((runtime / (session + '.pid')).read_text()), pid)
        stale = self.workspace()['live']
        self.assertEqual(stale['status'], 'stale')
        with self.assertRaises(HTTPError) as error:
            self.get(attachment['live']['stream_url'])
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(json.loads(command('eval', state_expression)['result']), after)
        attachment = self.post('/api/visual/attach', {'target': identity, 'name': 'Identity replacement test',
                                                    'session': session, 'port': port, 'pid': pid})
        stream = attachment['live']['stream_url']
        messages = queue.Queue()
        stop = threading.Event()
        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        try:
            next_frame()
            fixture = 'exec -a codex ' + shlex.join([sys.executable, str(self.root / 'fixture.py'),
                                                   str(self.root / 'live-replacement.input')])
            tmux(self.socket, 'respawn-pane', '-k', '-t', identity['pane'], fixture)
            deadline = time.monotonic() + 6
            identity_stale = False
            while time.monotonic() < deadline:
                event, data = messages.get(timeout=5)
                self.assertIn(event, ('status', 'frame'), data)
                if event == 'status' and data['status'] == 'stale':
                    identity_stale = True
                    break
            self.assertTrue(identity_stale, 'Replacement conversation must terminate the actual stream as stale')
        finally:
            stop.set()
            thread.join(7)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(command('eval', state_expression)['result']), after)
        output = Path(__file__).parent / '.run/visual-integration'
        output.mkdir(parents=True, exist_ok=True)
        (output / 'installed-browser.json').write_text(json.dumps({
            'passed': True, 'agent_browser_version': subprocess.check_output(['agent-browser', '--version'], text=True).strip(),
            'session': session, 'pid': pid, 'port': port, 'first_frame_hash': hashlib.sha256(first['data'].encode()).hexdigest(),
            'changed_frame_hash': hashlib.sha256(second['data'].encode()).hexdigest(),
            'relay_event_types': sorted(set(events)), 'viewport_before': before, 'viewport_after': after,
            'checks': ['actual HTTP attachment validation', 'actual native JPEG frame relay', 'first-open viewer attaches', 'real UI canvas follows agent change',
                       'tab switch clears live canvas', 'explicit reconnect resumes view', 'agent-controlled content change',
                       'other tab has no attachment', 'viewer disconnect preserves browser', 'detach preserves browser',
                       'same PID/port replacement listener becomes stale', 'old generation refuses reconnection', 'replaced conversation terminates active stream as stale'],
        }, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
