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

    def test_readable_terminal_scroll_position_survives_poll_and_append(self):
        self.browser_pages = {'a': [], 'b': []}
        pane = self.state['tabs'][0]['panes'][0]
        pane['terminal_ansi'] = '\n'.join(f'Output {i:03d} ' + 'x' * 230 for i in range(100))
        pane['terminal_ansi'] += '\n    Indented text  \t  with whitespace' + ' ' * 100 + '\n\x1b[32mhttps://example.test/' + 'long-path/' * 50 + '\x1b[0m'
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                page = self.page(mobile=mobile)
                terminal = page.locator('.terminal').first
                self.assertEqual(terminal.evaluate('e => parseFloat(getComputedStyle(e).fontSize)'), 12)
                self.assertTrue(terminal.evaluate('e => e.scrollHeight > e.clientHeight && e.scrollWidth <= e.clientWidth'))
                self.assertEqual(terminal.text_content(), pane['terminal_ansi'].replace('\x1b[32m', '').replace('\x1b[0m', ''))
                box = terminal.bounding_box()
                page.mouse.move(box['x'] + 30, box['y'] + 30)
                page.mouse.wheel(0, -350)
                page.wait_for_function('''() => {const e = document.querySelector('.terminal');
                  return e.scrollLeft === 0 && e.scrollHeight - e.scrollTop - e.clientHeight > 100;}''')
                position = terminal.evaluate('e => [e.scrollLeft, e.scrollTop]')
                page.evaluate('refresh()')
                self.assertEqual(terminal.evaluate('e => [e.scrollLeft, e.scrollTop]'), position)
                pane['terminal_ansi'] += '\nAppended wide line ' + 'y' * 250
                page.evaluate('refresh()')
                self.assertEqual(terminal.evaluate('e => [e.scrollLeft, e.scrollTop]'), position)
                terminal.evaluate('e => {e.scrollTop = e.scrollHeight - e.clientHeight - 12;}')
                position = terminal.evaluate('e => [e.scrollLeft, e.scrollTop]')
                pane['terminal_ansi'] += '\nAnother update'
                page.evaluate('refresh()')
                self.assertEqual(terminal.evaluate('e => [e.scrollLeft, e.scrollTop]'), position)
                terminal.evaluate('e => {e.scrollTop = e.scrollHeight;}')
                pane['terminal_ansi'] += '\nFollow this update'
                page.evaluate('refresh()')
                self.assertLessEqual(terminal.evaluate('e => e.scrollHeight - e.scrollTop - e.clientHeight'), 1)
                self.assertEqual(terminal.evaluate('e => e.scrollLeft'), position[0])
                if mobile:
                    page.locator('#tabs-toggle').click()
                    self.assertEqual(terminal.evaluate('e => parseFloat(getComputedStyle(e).fontSize)'), 12)
                    self.assertTrue(terminal.evaluate('e => e.scrollWidth <= e.clientWidth'))
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.assertEqual(page.evaluate('jarvis.target.identity'), 'a:original')
                page.close()
        self.assertFalse(any(path in ('/api/input', '/api/request', '/api/jarvis/turn') for path, _ in self.posts))

    def test_latest_tab_click_wins_while_previous_state_response_is_delayed(self):
        page = self.page()
        page.wait_for_function('visuals.browser.pages.length === 1')
        held = []
        def delay_b(route):
            if parse_qs(urlsplit(route.request.url).query).get('tab') == ['@b']:
                held.append(route)
            else:
                self.route(route)
        page.route('**/api/state?*', delay_b)
        page.locator('.tab[data-tab-id="@b"]').click()
        page.wait_for_function("currentState.selected === '@b' && selecting === '@b'")
        page.locator('.tab[data-tab-id="@a"]').click()
        try:
            page.wait_for_function("currentState.selected === '@a' && selecting === null", timeout=1500)
            page.wait_for_function("visuals.context?.tab === '@a' && visuals.browser.pages[0]?.page_id === 'page-a'")
        finally:
            page.unroute('**/api/state?*', delay_b)
            for route in held:
                route.fulfill(status=503, json={'error': 'Synthetic stale state failure'})
        page.wait_for_timeout(650)
        self.assertEqual(page.evaluate('currentState.selected'), '@a')
        self.assertEqual(page.evaluate('visuals.context.tab'), '@a')
        self.assertFalse(any(path in ('/api/select', '/api/input', '/api/request', '/api/jarvis/turn') for path, _ in self.posts))

    def test_old_poll_network_failure_cannot_disconnect_new_selection(self):
        page = self.page()
        page.wait_for_function('visuals.browser.pages.length === 1')
        held = []
        def delay_b(route):
            if parse_qs(urlsplit(route.request.url).query).get('tab') == ['@b']:
                held.append(route)
            else:
                self.route(route)
        page.route('**/api/state?*', delay_b)
        page.locator('.tab[data-tab-id="@b"]').click()
        page.wait_for_timeout(600)
        self.assertGreaterEqual(len(held), 2)
        page.locator('.tab[data-tab-id="@a"]').click()
        page.wait_for_function("visuals.context?.tab === '@a' && selecting === null")
        page.unroute('**/api/state?*', delay_b)
        for route in held:
            route.abort('failed')
        page.wait_for_timeout(100)
        self.assertEqual(page.locator('#connection').text_content(), 'Live')
        self.assertEqual(page.evaluate('visuals.context?.tab'), '@a')

    def test_rapid_pane_dropdown_return_preserves_browser_and_draft(self):
        second = copy.deepcopy(self.state['tabs'][0]['panes'][0])
        second.update(id='%a2', number=2, identity='a2:original', thread_id='a2', active=False, left=120)
        self.state['tabs'][0]['panes'].append(second)
        page = self.page()
        page.wait_for_function('visuals.browser.pages.length === 1')
        page.locator('#more-controls').evaluate('(element) => element.open = true')
        page.locator('#input-mode').select_option('direct')
        page.locator('#more-controls').evaluate('(element) => element.open = false')
        page.locator('#phone-text').fill('Keep  this draft.')
        captured = page.evaluate('voice.draft.target')
        held = []
        def delay_second(route):
            if parse_qs(urlsplit(route.request.url).query).get('pane') == ['%a2']:
                held.append(route)
            else:
                self.route(route)
        page.route('**/api/state?*', delay_second)
        page.locator('.visual-pane').select_option('%a2')
        page.wait_for_function("currentState.selected_pane === '%a2'")
        page.locator('.visual-pane').select_option('%a')
        try:
            page.wait_for_function("currentState.selected_pane === '%a' && selecting === null")
            page.wait_for_function("visuals.context?.pane === '%a' && visuals.browser.pages[0]?.page_id === 'page-a'")
        finally:
            page.unroute('**/api/state?*', delay_second)
            for route in held:
                self.route(route)
        page.wait_for_timeout(650)
        self.assertEqual(page.evaluate('currentState.selected_pane'), '%a')
        self.assertEqual(page.locator('#phone-text').input_value(), 'Keep  this draft.')
        self.assertEqual(page.evaluate('voice.draft.target'), captured)
        self.assertFalse(any(path in ('/api/input', '/api/request', '/api/jarvis/turn') for path, _ in self.posts))

    def test_two_clicks_before_terminal_input_finishes_keep_original_recipient(self):
        page = self.page()
        page.wait_for_function('visuals.browser.pages.length === 1')
        held = []
        page.route('**/api/input', lambda route: held.append(route))
        page.locator('.terminal[data-pane="%a"]').press('x')
        page.wait_for_function('pendingText === null')
        page.locator('.tab[data-tab-id="@b"]').click()
        page.locator('.tab[data-tab-id="@a"]').click()
        try:
            page.wait_for_function("currentState.selected === '@a' && selecting === null")
            self.assertEqual(len(held), 1)
            self.assertEqual(held[0].request.post_data_json, {'tab': '@a', 'pane': '%a', 'identity': 'a:original', 'text': 'x'})
        finally:
            page.unroute('**/api/input')
            for route in held:
                self.route(route)
        page.wait_for_timeout(650)
        self.assertEqual(page.evaluate('currentState.selected'), '@a')
        self.assertEqual([body['pane'] for path, body in self.posts if path == '/api/input'], ['%a'])

    def test_originating_jarvis_selection_supersedes_pending_tab_click(self):
        page = self.page()
        page.wait_for_function('jarvis.sessionId !== null && visuals.browser.pages.length === 1')
        held = []
        def delay_b(route):
            if parse_qs(urlsplit(route.request.url).query).get('tab') == ['@b']:
                held.append(route)
            else:
                self.route(route)
        def select_a(route):
            body = route.request.post_data_json
            self.posts.append(('/api/jarvis/turn', body))
            route.fulfill(json={'session_id': 's1', 'request_id': body['request_id'], 'turn_id': 'selection', 'revision': 2,
                                'status': 'completed', 'text': 'Selected A', 'actions': [],
                                'view_target': {'tab': '@a', 'pane': '%a', 'identity': 'a:original'}})
        page.route('**/api/state?*', delay_b)
        page.route('**/api/jarvis/turn', select_a)
        page.locator('.tab[data-tab-id="@b"]').click()
        page.wait_for_function("selecting === '@b'")
        page.locator('#phone-text').fill('Switch to A')
        page.locator('#phone-input [type=submit]').click()
        try:
            page.wait_for_function("currentState.selected === '@a' && selecting === null")
            page.wait_for_function("visuals.context?.tab === '@a' && visuals.browser.pages[0]?.page_id === 'page-a'")
        finally:
            page.unroute('**/api/state?*', delay_b)
            for route in held:
                self.route(route)
        page.wait_for_timeout(650)
        self.assertEqual(page.evaluate('currentState.selected'), '@a')
        self.assertEqual(len([path for path, _ in self.posts if path == '/api/jarvis/turn']), 1)
        self.assertFalse(any(path in ('/api/input', '/api/request') for path, _ in self.posts))

    def test_tabs_button_stays_reachable_after_header_focus_and_preview_changes(self):
        for title in ('Synthetic long session title for navigation geometry', 'A'):
            with self.subTest(title=title):
                self.state['tabs'][0]['name'] = title
                page = self.page(mobile=True)
                page.wait_for_function('visuals.browser.pages.length === 1')
                captured = page.evaluate('phoneTarget()')
                def header_button(selector):
                    geometry = page.locator(selector).evaluate("""element => {
                      const rect = element.getBoundingClientRect();
                      const workspace = document.getElementById('workspace');
                      return {x: rect.x, y: rect.y, width: rect.width, height: rect.height, right: rect.right,
                        viewport: innerWidth, hit: document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2) === element,
                        scroll: workspace.scrollLeft};
                    }""")
                    self.assertGreaterEqual(geometry['x'], 0)
                    self.assertLessEqual(geometry['right'], geometry['viewport'])
                    self.assertTrue(geometry['hit'])
                    self.assertEqual(geometry['scroll'], 0)
                    return geometry
                def click_header(selector):
                    rect = header_button(selector)
                    page.mouse.click(rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2)
                click_header('#tabs-toggle')
                page.keyboard.press('Tab')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'visual-toggle')
                header_button('#tabs-toggle')
                header_button('#visual-toggle')
                page.keyboard.press('Shift+Tab')
                click_header('#tabs-toggle')
                self.assertFalse(page.locator('#session-rail').is_visible())
                click_header('#visual-toggle')
                self.assertTrue(page.locator('#panes').is_visible())
                page.locator('#more-controls > summary').click()
                self.assertTrue(page.locator('#phone-escape').is_visible())
                page.locator('#more-controls > summary').click()
                click_header('#visual-toggle')
                for width, height in ((390, 360), (701, 844), (320, 568), (390, 844), (700, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    if width > 700:
                        self.assertFalse(page.locator('#tabs-toggle').is_visible())
                        self.assertTrue(page.locator('#session-rail').is_visible())
                        continue
                    click_header('#tabs-toggle')
                    self.assertTrue(page.locator('#session-rail').is_visible())
                    click_header('#tabs-toggle')
                    self.assertFalse(page.locator('#session-rail').is_visible())
                    header_button('#visual-toggle')
                self.assertEqual(page.evaluate('phoneTarget()'), captured)
                self.assertFalse(any(path in ('/api/input', '/api/request', '/api/jarvis/turn') for path, _ in self.posts))

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
