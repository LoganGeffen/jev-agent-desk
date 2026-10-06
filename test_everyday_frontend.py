import copy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from test_conversation_browser import fixture_state
from test_visual_workspace_browser import PIXEL, creation

ROOT = Path(__file__).parent


class EverydayFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.state = fixture_state()
        self.posts = []
        self.pages = []
        self.profile_status = 'ready'
        self.workspaces = {letter: {'workspace_id': 'workspace-' + letter, 'tab': '@' + letter, 'creations': [], 'live': None} for letter in ('a', 'b')}
        self.browser_pages = {letter: [{'page_id': 'page-' + letter, 'run_id': 'run', 'profile_id': 'desk', 'account_label': 'Desk account', 'account_epoch': 1, 'status': 'ready', 'url': 'https://example.test/' + letter, 'title': 'Browser ' + letter, 'workspace_id': 'workspace-' + letter, 'target': {'tab': '@' + letter, 'pane': '%' + letter, 'identity': letter + ':original'}, 'stream_url': '/api/browser/stream?page_id=page-' + letter + '&run_id=run'}] for letter in ('a', 'b')}
        self.errors = []

    def tearDown(self):
        for page in self.pages:
            page.close()
        self.assertEqual(self.errors, [])

    def page(self, mobile=False):
        page = self.browser.new_page(viewport={'width': 390 if mobile else 1440, 'height': 844 if mobile else 1000})
        page.set_default_timeout(5000)
        self.pages.append(page)
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.add_init_script('''
          window.observers = [];
          window.EventSource = class {
            constructor(url) { this.url = String(url); this.handlers = {}; this.closed = false; observers.push(this); }
            addEventListener(name, callback) { this.handlers[name] = callback; }
            close() { this.closed = true; }
            emit(name, body) { this.handlers[name]?.({data: JSON.stringify(body)}); }
          };
        ''')
        page.route('**/*', self.route)
        page.goto('https://everyday.test/')
        page.wait_for_function('currentState !== null')
        return page

    def route(self, route):
        parsed = urlsplit(route.request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        if route.request.method == 'POST':
            body = route.request.post_data_json
            self.posts.append((path, body))
            if path.startswith('/api/browser/'):
                operation = path.rsplit('/', 1)[-1]
                if operation == 'lease':
                    self.profile_status = 'human'
                    return route.fulfill(json={**self.browser_pages[body['target']['tab'][1:]][0], 'status': 'human', 'client_id': body['client_id'], 'lease_id': 'lease', 'expires_in': 20})
                if operation == 'done':
                    self.profile_status = 'ready'
                    return route.fulfill(json={'account_status': 'unknown', 'message': 'Control returned. Inspect the current account.'})
                if operation == 'disconnect':
                    self.profile_status = 'waiting'
                return route.fulfill(json={'accepted': True})
            if path == '/api/voice-event': return route.fulfill(json={})
            if path == '/api/input': return route.fulfill(json=self.state)
            if path == '/api/jarvis/turn': return route.fulfill(json={'session_id': 's1', 'request_id': body['request_id'], 'turn_id': 'turn', 'revision': 2, 'status': 'completed', 'text': 'Received', 'actions': []})
            return route.fulfill(json={})
        if path == '/api/state':
            state = copy.deepcopy(self.state)
            state['selected'] = query.get('tab', [state['selected']])[0]
            state['selected_pane'] = query.get('pane', [state['selected'].replace('@', '%')])[0]
            return route.fulfill(json=state)
        if path == '/api/jarvis/state': return route.fulfill(json={'session_id': 's1', 'revision': 1, 'subject': None, 'turns': {}, 'requests': {}, 'drafts': {}, 'actions': {}, 'jobs': {}, 'playbacks': {}, 'clarifications': {}})
        if path == '/api/visual/workspace': return route.fulfill(json=self.workspaces[query['tab'][0][1:]])
        if path == '/api/browser/state':
            pages = copy.deepcopy(self.browser_pages[query['tab'][0][1:]])
            for page in pages: page['status'] = self.profile_status
            return route.fulfill(json={'pages': pages, 'profiles': [{'profile_id': 'desk', 'account_label': 'Desk account', 'status': self.profile_status}]})
        if path == '/api/apps/state': return route.fulfill(json={'apps': []})
        if path == '/api/visual/archives': return route.fulfill(json={'workspaces': [{'workspace_id': 'archive-a', 'tab': 'Past work', 'closed': True, 'creations_count': 1}]})
        if path == '/api/visual/archive': return route.fulfill(json={'workspace_id': 'archive-a', 'tab': None, 'archived': True, 'creations': [creation('text')], 'live': None})
        if path == '/api/visual/content': return route.fulfill(body='Saved original report\n' * 100, content_type='text/plain')
        if path.startswith('/visual-preview/'): return route.fulfill(body='<h1>Saved prototype</h1>', content_type='text/html', headers={'Content-Security-Policy': "sandbox allow-scripts; default-src 'none'"})
        file = ROOT / ('index.html' if path == '/' else path.lstrip('/'))
        if file.is_file() and (path == '/' or file.suffix in ('.js', '.mjs', '.css')):
            return route.fulfill(body=file.read_bytes(), content_type='text/html' if path == '/' else 'text/css' if file.suffix == '.css' else 'text/javascript')
        return route.fulfill(status=404, body='Not found')

    def test_devices_drafts_auto_open_history_and_reload(self):
        desktop = self.page()
        phone = self.page(mobile=True)
        desktop.wait_for_function('visuals.browser.pages.length === 1')
        phone.wait_for_function('visuals.browser.pages.length === 1')
        phone.evaluate("selectTab('@b')")
        phone.wait_for_function("currentState.selected === '@b'")
        desktop.wait_for_timeout(600)
        self.assertEqual(desktop.evaluate('currentState.selected'), '@a')
        self.assertFalse(any(path == '/api/select' for path, _ in self.posts))
        desktop.locator('#more-controls').evaluate('(element) => element.open = true')
        desktop.locator('#input-mode').select_option('direct')
        desktop.locator('#more-controls').evaluate('(element) => element.open = false')
        desktop.locator('#phone-text').fill('Keep  these exact words.')
        captured = desktop.evaluate('voice.draft.target')
        phone.evaluate("selectTab('@a')")
        self.assertEqual(desktop.evaluate('voice.draft.target'), captured)
        desktop.locator('.visual-back').evaluate('(button) => button.click()')
        self.workspaces['a']['creations'] = [creation('text', 2)]
        desktop.evaluate('() => { visuals.lastRefresh = 0; visuals.refresh(); }')
        desktop.wait_for_function("document.getElementById('visual-toggle').textContent.includes('new')")
        self.assertFalse(desktop.locator('#visual-workspace').is_visible())
        desktop.locator('#visual-toggle').click()
        desktop.locator('#visual-creations-tab').click()
        desktop.locator('.visual-version').select_option('text-1')
        self.workspaces['a']['creations'] = [creation('text', 3)]
        desktop.evaluate('() => { visuals.lastRefresh = 0; visuals.refresh(); }')
        desktop.wait_for_function("visuals.workspace.creations[0].versions.length === 3")
        self.assertEqual(desktop.locator('.visual-version').input_value(), 'text-1')
        desktop.reload()
        desktop.wait_for_selector('.visual-preview pre')
        self.assertEqual(desktop.locator('.visual-version').input_value(), 'text-1')
        self.assertEqual(desktop.locator('#phone-text').input_value(), 'Keep  these exact words.')
        phone.evaluate("selectTab('@b')")
        phone.reload()
        phone.wait_for_function("currentState?.selected === '@b'")
        self.assertEqual(desktop.evaluate('currentState.selected'), '@a')
        self.assertFalse(phone.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_human_input_is_direct_lease_scoped_and_local_zoom_is_passive(self):
        page = self.page(mobile=True)
        page.wait_for_function('visuals.browser.pages.length === 1')
        page.locator('#visual-live-tab').click()
        page.wait_for_function('observers.length > 0')
        import base64
        page.evaluate('(data) => observers.at(-1).emit("frame", {page_id:"page-a",run_id:"run",sequence:1,mime:"image/png",data})', base64.b64encode(PIXEL).decode())
        page.wait_for_selector('.browser-stage canvas:visible')
        page.locator('.browser-zoom').select_option('2')
        self.assertFalse(any(path == '/api/browser/input' for path, _ in self.posts))
        page.locator('.browser-signin').click()
        page.wait_for_selector('.browser-done:visible')
        page.locator('.browser-typing').fill('synthetic-otp-123456', force=True)
        page.wait_for_function('visuals.browser.sequence === 1')
        page.locator('.browser-enter').click()
        page.locator('.browser-done').click()
        page.wait_for_selector('.browser-done', state='hidden')
        inputs = [body for path, body in self.posts if path == '/api/browser/input']
        self.assertEqual([body['sequence'] for body in inputs], [1, 2])
        self.assertEqual(inputs[0]['args']['text'], 'synthetic-otp-123456')
        self.assertTrue(all(body['lease_id'] == 'lease' and body['target']['identity'] == 'a:original' for body in inputs))
        self.assertFalse(any(path in ('/api/input', '/api/jarvis/turn', '/api/request') for path, _ in self.posts))
        self.assertEqual(page.locator('.browser-typing').input_value(), '')

    def test_closed_archive_is_read_only_and_returns_to_current_tab(self):
        page = self.page()
        page.wait_for_function('visuals.browser.pages.length === 1')
        page.locator('.visual-archives').click()
        page.get_by_role('button', name='Past work · 1 creations').click()
        page.locator('.visual-preview pre').wait_for()
        self.assertTrue(page.locator('.visual-archive-banner').is_visible())
        self.assertEqual(page.evaluate('currentState.selected'), '@a')
        page.get_by_role('button', name='Return to live tab').click()
        page.wait_for_function('visuals.workspace?.workspace_id === "workspace-a"')
        self.assertFalse(page.locator('.visual-archive-banner').is_visible())


if __name__ == '__main__':
    unittest.main()
