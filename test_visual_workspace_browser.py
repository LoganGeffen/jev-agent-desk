import base64
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aIh8AAAAASUVORK5CYII=')


def selection(tab='a', identity=None):
    return {'selected': '@' + tab, 'selected_pane': '%' + tab,
            'tabs': [{'id': '@' + tab, 'name': tab.upper(),
                      'panes': [{'id': '%' + tab, 'identity': identity or tab + ':original'}]}]}


def creation(kind, number=1):
    def version(value):
        return {'id': f'{kind}-{value}', 'number': value, 'created_at': '2026-10-06T14:00:00Z',
                'entrypoint': 'index.html', 'byte_size': 2048,
                'source': {'mode': 'captured', 'provider': 'codex', 'thread_id': 'conversation-a', 'pane': '%a'},
                'download_url': f'/api/visual/download?version={kind}-{value}',
                'content_url': f'/api/visual/content?version={kind}-{value}',
                'preview_url': f'/visual-preview/{kind}-{value}/index.html'}
    return {'id': kind, 'title': kind.title() + ' creation', 'kind': kind,
            'versions': [version(value) for value in range(number, 0, -1)]}


class VisualWorkspaceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.contents = {'markdown-1': '# Saved notes\n\n- First point\n- **Second** point\n\n```js\nconst safe = true;\n```\n\n[External](https://example.invalid/)\n\n<img src="https://example.invalid/leak"><script>parent.pwned = true</script>', 'text-1': '<script>literal source</script>'}
        self.workspaces = {
            '@a': {'workspace_id': 'workspace-a', 'tab': '@a', 'creations': [creation('html', 2), creation('markdown'), creation('text'), creation('image'), creation('pdf')],
                   'live': {'id': 'browser-a', 'generation': 'generation-a', 'status': 'ready', 'name': 'Browser A', 'pane': '%a', 'identity': 'a:original', 'session': 'session-a', 'stream_url': '/api/visual/live?attachment=browser-a'}},
            '@b': {'workspace_id': 'workspace-b', 'tab': '@b', 'creations': [], 'live': None}}
        self.requests = []
        self.page.add_init_script('''
          window.streams = [];
          window.EventSource = class {
            constructor(url) { this.url = url; this.handlers = {}; this.closed = false; streams.push(this); }
            addEventListener(name, callback) { this.handlers[name] = callback; }
            close() { this.closed = true; }
            emit(name, body) { this.handlers[name]?.({data: JSON.stringify(body)}); }
          };
        ''')
        self.page.route('**/*', self.route)
        self.page.goto('http://visual.test/')
        self.page.evaluate('(state) => view.update(state)', selection())
        self.page.wait_for_selector('.visual-preview iframe')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.page.close()

    def route(self, route):
        parsed = urlsplit(route.request.url)
        self.requests.append(parsed.path)
        query = parse_qs(parsed.query)
        if parsed.path == '/':
            return route.fulfill(content_type='text/html', body='''<!doctype html><html><head>
              <style>:root{--line:#26312c;--line-strong:#3b4a43;--muted:#8d9b94;--accent:#9de2bc;--panel:#0e1211;--attention:#ffc66d}body{margin:0;background:#090c0b;color:#e6ece8;font:14px system-ui}#workspace-body{height:700px}#panes{min-height:0}</style>
              <link rel="stylesheet" href="/visual-workspace.css"></head><body>
              <button id="visual-toggle">Visuals</button><div id="workspace-body"><div id="panes">Terminal</div><section id="visual-workspace"></section></div>
              <script src="/vendor/marked.min.js"></script><script src="/vendor/purify.min.js"></script><script src="/visual-workspace.js"></script>
              <script>const view = new VisualWorkspace(document.getElementById('visual-workspace'), document.getElementById('visual-toggle'));</script></body></html>''')
        if parsed.path == '/api/visual/workspace':
            return route.fulfill(json=self.workspaces[query['tab'][0]])
        if parsed.path == '/api/visual/content':
            version = query['version'][0]
            if version.startswith('image'):
                return route.fulfill(body=PIXEL, content_type='image/png')
            if version.startswith('pdf'):
                return route.fulfill(body=b'%PDF-1.4\n%%EOF', content_type='application/pdf')
            return route.fulfill(body=self.contents[version], content_type='text/plain')
        if parsed.path.startswith('/visual-preview/'):
            return route.fulfill(body=f'<h1>Frozen {parsed.path.split("/")[2]}</h1>', content_type='text/html',
                                 headers={'Content-Security-Policy': "sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'"})
        path = ROOT / parsed.path.lstrip('/')
        if parsed.path in ['/visual-workspace.css', '/visual-workspace.js', '/vendor/marked.min.js', '/vendor/purify.min.js']:
            return route.fulfill(body=path.read_bytes(), content_type='text/css' if path.suffix == '.css' else 'text/javascript')
        return route.fulfill(status=404, body='Not found')

    def test_saved_renderers_versions_and_isolation(self):
        frame = self.page.locator('.visual-preview iframe')
        self.assertEqual(frame.get_attribute('sandbox'), 'allow-scripts')
        self.assertNotIn('allow-same-origin', frame.get_attribute('sandbox'))
        self.page.locator('.visual-version').select_option('html-1')
        self.page.frame_locator('.visual-preview iframe').get_by_text('Frozen html-1').wait_for()
        self.page.evaluate('() => { view.lastRefresh = 0; view.refresh(); }')
        self.assertEqual(self.page.locator('.visual-version').input_value(), 'html-1')
        self.assertIn('version=html-1', self.page.locator('.visual-download').get_attribute('href'))
        self.page.locator('.visual-creation').select_option('markdown')
        markdown = self.page.frame_locator('.visual-preview iframe')
        markdown.get_by_role('heading', name='Saved notes').wait_for()
        self.assertEqual(markdown.locator('li').count(), 2)
        self.assertEqual(markdown.locator('pre code').inner_text().strip(), 'const safe = true;')
        self.assertEqual(markdown.locator('script, img, a[href]').count(), 0)
        self.assertEqual(self.page.locator('.visual-preview iframe').get_attribute('sandbox'), '')
        self.assertFalse(self.page.evaluate('Boolean(window.pwned)'))
        self.assertFalse(any('/leak' in request for request in self.requests))
        self.page.locator('.visual-creation').select_option('text')
        self.page.locator('.visual-preview pre').wait_for()
        self.assertEqual(self.page.locator('.visual-preview pre').inner_text(), '<script>literal source</script>')
        self.page.locator('.visual-creation').select_option('image')
        self.page.wait_for_function('document.querySelector(".visual-preview img")?.naturalWidth === 1')
        self.page.locator('.visual-creation').select_option('pdf')
        self.page.locator('.visual-preview object').wait_for()
        self.assertIn('PDF reader', self.page.locator('.visual-preview > p').inner_text())
        self.page.evaluate('(state) => view.update(state)', selection('b'))
        self.page.get_by_text('No creations saved yet').wait_for()
        self.assertEqual(self.page.locator('.visual-preview iframe, .visual-preview object, .visual-preview img').count(), 0)
        self.assertFalse(self.page.locator('.visual-controls').is_visible())

    def test_delayed_metadata_and_content_cannot_cross_selection(self):
        self.page.evaluate('''() => {
          const actual = window.fetch;
          window.fetch = (url, opts) => String(url).includes('/api/visual/workspace')
            ? new Promise(resolve => window.releaseOldWorkspace = () => resolve(new Response(JSON.stringify({workspace_id: 'workspace-a', tab: '@a', creations: [], live: null}))))
            : actual(url, opts);
          view.lastRefresh = 0; view.refresh();
          window.actualFetch = actual;
        }''')
        self.page.wait_for_function('Boolean(window.releaseOldWorkspace)')
        self.page.evaluate('(state) => { window.fetch = window.actualFetch; view.update(state); }', selection('b'))
        self.page.get_by_text('No creations saved yet').wait_for()
        self.page.evaluate('releaseOldWorkspace()')
        self.assertEqual(self.page.evaluate('view.workspace.workspace_id'), 'workspace-b')
        self.page.evaluate('(state) => view.update(state)', selection())
        self.page.locator('.visual-creation').wait_for()
        self.page.evaluate('''() => {
          const actual = window.fetch;
          window.fetch = (url, opts) => String(url).includes('version=text-1')
            ? new Promise(resolve => window.releaseOldText = () => resolve(new Response('OLD CONVERSATION CONTENT')))
            : actual(url, opts);
        }''')
        self.page.locator('.visual-creation').select_option('text')
        self.page.wait_for_function('Boolean(window.releaseOldText)')
        self.page.evaluate('(state) => view.update(state)', selection('b'))
        self.page.get_by_text('No creations saved yet').wait_for()
        self.page.evaluate('releaseOldText()')
        self.assertNotIn('OLD CONVERSATION CONTENT', self.page.locator('.visual-preview').inner_text())

    def test_live_generation_disconnect_and_pending_decode_fences(self):
        self.page.locator('#visual-live-tab').click()
        self.assertEqual(self.page.evaluate('streams.length'), 1)
        self.page.evaluate('''() => {
          window.pendingImages = [];
          window.Image = class { constructor() { this.naturalWidth = 1; this.naturalHeight = 1; pendingImages.push(this); } };
          window.paints = 0;
          view.canvas.getContext = () => ({drawImage() { window.paints += 1; }});
          streams[0].emit('frame', {generation:'wrong', seq:1, mime:'image/png',data:'abc'});
        }''')
        self.assertEqual(self.page.evaluate('pendingImages.length'), 0)
        self.page.evaluate("streams[0].emit('frame', {generation:'generation-a', seq:1, mime:'image/png',data:'abc'})")
        self.assertEqual(self.page.evaluate('pendingImages.length'), 1)
        self.page.evaluate('(state) => view.update(state)', selection('b'))
        self.assertTrue(self.page.evaluate('streams[0].closed'))
        self.page.evaluate('pendingImages[0].onload()')
        self.assertEqual(self.page.evaluate('paints'), 0)
        self.assertFalse(self.page.locator('canvas').is_visible())

        self.page.evaluate('(state) => view.update(state)', selection())
        self.page.wait_for_function('streams.length === 2')
        self.page.evaluate("streams[1].emit('frame', {generation:'generation-a', seq:2, mime:'image/png',data:'abc'}); pendingImages[1].onload();")
        self.assertEqual(self.page.evaluate('paints'), 1)
        self.assertTrue(self.page.locator('canvas').is_visible())
        self.page.evaluate("streams[1].emit('status', {generation:'generation-a', status:'disconnected', error:'Socket closed'})")
        self.assertFalse(self.page.locator('canvas').is_visible())
        self.assertTrue(self.page.evaluate('streams[1].closed'))
        self.assertEqual(self.page.locator('.visual-status').inner_text(), 'Socket closed')
        self.page.evaluate('view.renderLive()')
        self.assertEqual(self.page.evaluate('streams.length'), 2)
        self.page.locator('.visual-retry').click()
        self.page.wait_for_function('streams.length === 3')
        self.page.evaluate('(state) => view.update(state)', selection(identity='a:replaced'))
        self.assertTrue(self.page.evaluate('streams[2].closed'))
        self.page.get_by_text('This browser belongs to another pane. Select its pane to view it.', exact=True).first.wait_for()
        self.assertEqual(self.page.evaluate('streams.length'), 3)
        self.assertFalse(self.page.locator('canvas').is_visible())

    def test_disconnected_metadata_manual_retry_and_stale_latch(self):
        self.workspaces['@a']['live']['status'] = 'disconnected'
        self.page.evaluate("() => { view.workspace.live.status = 'disconnected'; }")
        self.page.locator('#visual-live-tab').click()
        self.assertEqual(self.page.evaluate('streams.length'), 0)
        self.page.locator('.visual-retry').click()
        self.page.wait_for_function('streams.length === 1')
        self.page.evaluate('view.renderLive()')
        self.assertFalse(self.page.evaluate('streams[0].closed'))
        self.page.evaluate("streams[0].emit('status', {generation:'generation-a', status:'disconnected', error:'Socket closed'})")
        self.assertTrue(self.page.evaluate('streams[0].closed'))
        self.page.evaluate('view.renderLive()')
        self.page.locator('#visual-toggle').click()
        self.page.locator('#visual-toggle').click()
        self.assertEqual(self.page.evaluate('streams.length'), 1)
        self.page.locator('.visual-retry').click()
        self.page.wait_for_function('streams.length === 2')
        self.page.evaluate("streams[1].emit('status', {generation:'generation-a', status:'stale', error:'Browser process replaced'})")
        self.page.evaluate('view.renderLive()')
        self.assertEqual(self.page.locator('.visual-status').get_attribute('data-status'), 'stale')
        self.assertFalse(self.page.locator('.visual-retry').is_visible())
        self.workspaces['@a']['live']['status'] = 'ready'
        self.page.evaluate("() => { view.workspace.live.status = 'ready'; view.renderLive(); }")
        self.page.locator('#visual-toggle').click()
        self.page.locator('#visual-toggle').click()
        self.assertEqual(self.page.evaluate('streams.length'), 2)
        self.page.evaluate("() => { view.workspace.live.generation = 'new-generation'; view.renderLive(); }")
        self.assertEqual(self.page.evaluate('streams.length'), 3)


if __name__ == '__main__':
    unittest.main()
