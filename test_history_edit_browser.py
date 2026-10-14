from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


class HistoryEditBrowserTests(unittest.TestCase):
    def test_failed_edit_retains_exact_draft_and_never_replays(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.route('http://localhost/', lambda route: route.fulfill(body='<div id="history"></div>', content_type='text/html'))
            page.goto('http://localhost/')
            page.add_script_tag(path=str(Path(__file__).with_name('terminal.js')))
            page.evaluate('''() => {
              window.requests = [];
              window.reader = new ConversationHistory(document.querySelector('#history'), async (url, body) => {
                requests.push({url, body});
                throw new Error('connection lost');
              });
              reader.target = {tab: '@a', pane: '%a', identity: 'original'};
              reader.identity = 'original';
              window.node = document.createElement('article'); reader.element.append(node);
              reader.renderTurn(node, {id: 'turn', items: [{id: 'item', type: 'userMessage',
                content: [{type: 'text', text: 'Original  wording'}]}]});
            }''')
            page.get_by_role('button', name='Edit', exact=True).click()
            page.get_by_role('textbox', name='Edited message').fill('Revised  wording\nsecond line')
            page.get_by_role('button', name='Submit edit').click()
            page.wait_for_function('requests.length === 1 && document.querySelector("dialog p:last-of-type").textContent.includes("connection lost")')
            self.assertTrue(page.get_by_role('button', name='Submit edit').is_disabled())
            request = page.evaluate('requests[0].body')
            self.assertEqual(request['target']['identity'], 'original')
            self.assertEqual(request['arguments'], {'turn_id': 'turn', 'item_id': 'item',
                'original_text': 'Original  wording', 'text': 'Revised  wording\nsecond line'})
            page.get_by_role('button', name='Close', exact=True).click()
            page.evaluate('reader.target = {...reader.target, identity: "replacement"}; reader.openEdit(reader.savedEdit())')
            self.assertEqual(page.get_by_role('textbox', name='Edited message').input_value(), 'Revised  wording\nsecond line')
            self.assertTrue(page.get_by_role('button', name='Submit edit').is_disabled())
            self.assertEqual(page.evaluate('requests.length'), 1)
            browser.close()


if __name__ == '__main__':
    unittest.main()
