from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


class HistoryDetailsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_lazy_details_preserve_nodes_scroll_and_ignore_stale_response(self):
        page = self.browser.new_page(viewport={'width': 390, 'height': 844})
        try:
            page.set_content('<div id="history" style="height:400px;overflow:auto"></div>')
            page.add_script_tag(path=str(Path(__file__).with_name('terminal.js')))
            page.evaluate('''() => {
              window.requests = [];
              window.reader = new ConversationHistory(document.getElementById('history'), (url, body) => {
                requests.push({url, body});
                return new Promise(resolve => { window.resolveDetails = resolve; });
              });
              reader.target = {tab: '@a', pane: '%a', identity: 'original'};
              reader.identity = 'original';
              window.turn = {id: 't', status: 'completed', items: [
                {type: 'userMessage', content: [{type: 'text', text: 'Exact  message'},
                  {type: 'localImage', path: '/not/fetched.png', availability: 'Metadata only'}]},
                {type: 'mcpToolCall', tool: 'test', preview: 'preview', shortened: true,
                  detail_ref: {before: 'older', turn_id: 't', item_index: 1, item_key: 'key'}}]};
              window.node = document.createElement('article');
              node.className = 'history-turn';
              reader.element.append(node);
              reader.renderTurn(node, turn);
            }''')
            self.assertEqual(page.evaluate('requests.length'), 0)
            self.assertIn('/not/fetched.png', page.locator('.user').inner_text())
            self.assertEqual(page.locator('img').count(), 0)
            self.assertEqual(page.locator('.history-turn-status').inner_text(), 'Turn: completed')
            page.locator('summary').click()
            self.assertEqual(page.evaluate('requests.length'), 0)
            page.locator('button').click()
            request = page.evaluate('requests[0]')
            self.assertEqual(request['url'], '/api/history/details')
            self.assertEqual(request['body'], {'tab': '@a', 'pane': '%a', 'identity': 'original',
                'before': 'older', 'turn_id': 't', 'item_index': 1, 'item_key': 'key', 'offset': 0})
            top = page.locator('summary').evaluate('e => e.getBoundingClientRect().top')
            page.evaluate('''() => resolveDetails({identity: 'original', item_key: 'key', offset: 0,
              text: 'line\\n'.repeat(1000), nextOffset: 5000, total: 5004})''')
            page.wait_for_function('document.querySelector("pre").textContent.length === 5000')
            self.assertAlmostEqual(top, page.locator('summary').evaluate('e => e.getBoundingClientRect().top'), delta=1)
            page.evaluate('window.originalBody = document.querySelector("pre"); reader.renderTurn(node, turn)')
            self.assertTrue(page.evaluate('originalBody === document.querySelector("pre")'))
            self.assertTrue(page.locator('details').evaluate('e => e.open'))
            page.locator('button').click()
            self.assertEqual(page.evaluate('requests[1].body.offset'), 5000)
            page.evaluate('''() => resolveDetails({identity: 'original', item_key: 'key', offset: 5000,
              text: 'TAIL', nextOffset: null, total: 5004})''')
            page.wait_for_function('document.querySelector("button").disabled')
            self.assertTrue(page.locator('pre').text_content().endswith('TAIL'))
            page.evaluate('''() => {
              turn.items[1].preview = 'changed';
              reader.renderTurn(node, turn);
            }''')
            page.locator('summary').click()
            page.locator('button').click()
            page.evaluate('''() => {
              reader.target = {...reader.target, identity: 'replacement'};
              resolveDetails({identity: 'original', item_key: 'key', offset: 0,
                text: 'STALE', nextOffset: null, total: 5});
            }''')
            page.wait_for_timeout(50)
            self.assertEqual(page.locator('pre').text_content(), 'changed')
        finally:
            page.close()
