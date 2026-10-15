from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


class TabHistoryBrowserTests(unittest.TestCase):
    def test_switch_retains_history_scroll_and_refreshes_without_loading_flash(self):
        root = Path(__file__).parent
        source = (root / 'index.html').read_text()
        render = source[source.index('    const paneViews ='):source.index('    async function closePane')]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.set_content('<style>.history-view{height:300px;overflow:auto}.pane{width:380px}.terminal[hidden]{display:none}</style><div id="panes"></div>')
            page.add_script_tag(path=str(root / 'terminal.js'))
            page.add_script_tag(content='''
              const $ = id => document.getElementById(id);
              let activeTab = null;
              window.pending = []; window.requests = [];
              const post = (url, body) => {
                requests.push(body);
                return new Promise(resolve => pending.push(() => resolve({identity: body.identity,
                  turns: [{id: body.identity, items: [{type: 'userMessage', content: [{type: 'text', text: 'line\\n'.repeat(100)}]}]}], before: null})));
              };
              const showError = error => { throw error; };
              window.currentState = {selected: '@a', tabs: ['a','b'].map(id => ({id: '@'+id, name: id,
                panes: [{id: '%'+id, number: 1, active: true, identity: id, thread_id: id, provider: 'codex',
                  command: 'codex', conversation_history: true, width: 100, height: 24, left: 0, top: 0, handoff: {}}]}))};
            ''' + render)
            page.evaluate('renderPanes(currentState); pending.shift()()')
            page.wait_for_function('document.querySelector(".history-turn") !== null')
            page.evaluate('''() => {
              window.original = document.querySelector('.history-view');
              original.style.whiteSpace = 'pre-wrap'; original.scrollTop = 160;
              window.scrollBefore = original.scrollTop;
              currentState.selected = '@b'; renderPanes(currentState); pending.shift()();
            }''')
            page.wait_for_function('document.querySelector(".history-turn") !== null')
            page.evaluate('''() => {
              paneViews.get('@a:%a').history.lastFetch = 0;
              currentState.selected = '@a'; renderPanes(currentState);
            }''')
            self.assertTrue(page.evaluate('document.querySelector(".history-view") === original'))
            self.assertEqual(page.evaluate('original.scrollTop'), page.evaluate('scrollBefore'))
            self.assertNotIn('Loading conversation', page.locator('#panes').inner_text())
            self.assertEqual(page.evaluate('requests.length'), 3)
            page.evaluate('pending.shift()()')
            page.wait_for_function('paneViews.get("@a:%a").history.loading === null')
            self.assertEqual(page.evaluate('original.scrollTop'), page.evaluate('scrollBefore'))
            page.evaluate('''() => {
              currentState.selected = '@b'; renderPanes(currentState);
              currentState.tabs[0].panes[0].identity = 'replacement'; renderPanes(currentState);
              currentState.selected = '@a'; renderPanes(currentState);
            }''')
            self.assertFalse(page.evaluate('document.querySelector(".history-view") === original'))
            self.assertEqual(page.locator('.history-turn').count(), 0)
            self.assertIn('Loading conversation', page.locator('#panes').inner_text())
            page.evaluate('currentState.tabs = [currentState.tabs[0]]; renderPanes(currentState)')
            self.assertEqual(page.evaluate('paneViews.size'), 1)
            browser.close()


if __name__ == '__main__':
    unittest.main()
