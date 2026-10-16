from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright


class LiveActivityBrowserTests(unittest.TestCase):
    def test_native_activity_subagent_output_and_terminal_keep_scroll(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.set_content('<div id="history" style="height:400px;overflow:auto"></div>')
            page.add_script_tag(path=str(Path(__file__).with_name('terminal.js')))
            page.evaluate('''() => {
              window.requests = [];
              window.reader = new ConversationHistory(document.querySelector('#history'), (url, body) => {
                requests.push(body);
                if (body.agent_id) return new Promise(resolve => window.finishChild = resolve);
                return Promise.resolve({identity: body.identity, turns: [{id: 'turn', status: 'interrupted',
                  items: [{type: 'userMessage', content: [{type:'text', text:'long history '.repeat(600)}]}]}]});
              });
              window.target = {tab:'@1', pane:'%1', identity:'one'};
              window.activity = {state:'working', turn_id:'turn',
                tools:[{name:'read_file',status:'running'}, {name:'clock.sleep',status:'running'}],
                recent:[{name:'sleep',status:'returned'}, {name:'exec',status:'returned'}],
                agents:[{id:'child',name:'/root/checks',state:'working'}]};
              reader.update(target, true, activity, 'Native Working…');
            }''')
            page.wait_for_function('reader.loaded')
            self.assertEqual(page.locator('.conversation-activity > summary').inner_text(), 'Working…')
            self.assertNotIn('Turn:', page.locator('#history').inner_text())
            self.assertIn('read_file · running', page.locator('.conversation-activity').inner_text())
            self.assertNotIn('sleep', page.locator('.conversation-activity').inner_text())
            self.assertNotIn('returned', page.locator('.conversation-activity').inner_text())
            self.assertIn('checks · Working…', page.locator('.conversation-activity').inner_text())
            page.get_by_text('checks · Working…', exact=True).click()
            page.wait_for_function('requests.some(request => request.agent_id === "child")')
            page.evaluate('finishChild({identity:"one", turns:[{items:[{type:"agentMessage",text:"Checked actual files"}]}]})')
            page.wait_for_function('document.querySelector(".conversation-activity").textContent.includes("Checked actual files")')
            page.get_by_text('View terminal', exact=True).click()
            expect(page.locator('.activity-terminal')).to_have_text('Native Working…')
            page.evaluate('''() => {
              reader.element.scrollTop = 100;
              window.topBefore = reader.element.scrollTop;
              activity.tools = [{name:'tests',status:'running'}];
              reader.update(target, true, activity, 'Native tool running');
            }''')
            self.assertEqual(page.evaluate('reader.element.scrollTop'), page.evaluate('topBefore'))
            page.evaluate('reader.update(target, true, {state:"ready", agents:[{id:"child",name:"/root/checks",state:"working"}]}, "")')
            self.assertEqual(page.locator('.conversation-activity > summary').inner_text(), 'Working…')
            page.evaluate('reader.update(target, true, {state:"ready", agents:[{id:"child",name:"/root/checks",state:"ready"}]}, "")')
            self.assertFalse(page.locator('.conversation-activity').evaluate('node => node.open'))
            page.evaluate('reader.update(target, true, {state:"unknown",reason:"Connection lost"}, "")')
            self.assertIn('Activity unavailable', page.locator('.conversation-activity').inner_text())
            self.assertIn('Connection lost', page.locator('.conversation-activity').inner_text())

    def test_active_history_refreshes_faster_and_completion_refreshes_immediately(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<div id="history"></div>')
            page.add_script_tag(path=str(Path(__file__).with_name('terminal.js')))
            page.evaluate('''() => {
              window.now = 10000; Date.now = () => now;
              window.requests = [];
              window.reader = new ConversationHistory(document.querySelector('#history'), (url, body) => {
                requests.push(body);
                return new Promise(resolve => window.finish = () => resolve({identity: body.identity, turns: []}));
              });
              window.target = {tab:'@1',pane:'%1',identity:'one'};
              reader.update(target, true, {state:'working'}, '');
            }''')
            page.evaluate("now += 1000; reader.update(target, true, {state:'working'}, '')")
            self.assertEqual(page.evaluate('requests.length'), 1)
            page.evaluate('finish()')
            page.wait_for_function('!reader.loading')
            page.evaluate("reader.update(target, true, {state:'working'}, '')")
            self.assertEqual(page.evaluate('requests.length'), 2)
            page.evaluate('finish()')
            page.wait_for_function('!reader.loading')
            page.evaluate("now += 100; reader.update(target, true, {state:'ready'}, '')")
            self.assertEqual(page.evaluate('requests.length'), 3)
            page.evaluate('finish()')
            page.wait_for_function('!reader.loading')
            page.evaluate("now += 1000; reader.update(target, true, {state:'ready'}, '')")
            self.assertEqual(page.evaluate('requests.length'), 3)
            page.evaluate("now += 2000; reader.update(target, true, {state:'ready'}, '')")
            self.assertEqual(page.evaluate('requests.length'), 4)
            browser.close()

    def test_late_subagent_response_cannot_fill_replacement_conversation(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content('<div id="history"></div>')
            page.add_script_tag(path=str(Path(__file__).with_name('terminal.js')))
            page.evaluate('''() => {
              window.reader = new ConversationHistory(document.querySelector('#history'), (url, body) => {
                if (body.agent_id) return new Promise(resolve => window.finishChild = resolve);
                return Promise.resolve({identity: body.identity, turns: []});
              });
              reader.update({tab:'@1',pane:'%1',identity:'old'}, true,
                {state:'working',agents:[{id:'child',name:'checks',state:'working'}]}, '');
            }''')
            page.get_by_text('checks · Working…', exact=True).click()
            page.wait_for_function('typeof finishChild === "function"')
            page.evaluate('''() => {
              reader.update({tab:'@1',pane:'%1',identity:'new'}, true, {state:'idle'}, '');
              finishChild({identity:'old',turns:[{items:[{type:'agentMessage',text:'WRONG OUTPUT'}]}]});
            }''')
            page.wait_for_function('reader.loaded')
            self.assertNotIn('WRONG OUTPUT', page.locator('#history').inner_text())
