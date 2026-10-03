import json
from pathlib import Path
import threading
import unittest

from playwright.sync_api import sync_playwright
from server import make_server
from test_jarvis_tools import DisposableDesk, final


class RealBrowserTests(DisposableDesk):
    def test_mobile_page_uses_actual_http_controller_and_terminal_delivery(self):
        if not Path(__file__).with_name('jarvis.js').exists():
            self.skipTest('Frontend commit must be integrated for the real-page gate')
        request = 'Ask it to explain those numbers with an example'
        body = 'explain those numbers with an example'
        self.allow(request, body=body)
        contexts = []
        def provider(context, specs, execute, **kwargs):
            contexts.append(context)
            text = context['current_turn']['user_text']
            if text == 'What is ready?':
                execute('list_ready', {})
            elif text == 'Read Dog Data':
                execute('read_reply', {'name': 'Dog Data'})
            elif text == request:
                draft = execute('prepare_literal', {})
                execute('send_draft', {'draft_id': draft['draft_id']})
            else:
                execute('read_reply', {'name': 'Controller'})
            return final('Untrusted model: sent and all done.')
        self.provider = provider
        for name in ('Dog Data', 'Controller'):
            agent = self.app.jarvis_tools.agent(self.targets[name])
            self.app.attention.observe(agent, self.history(agent['thread_id']))
        self.app.focus(self.targets['Notes']['tab'])
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors, scripts, requests = [], [], []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: scripts.append(response.status) if response.url.endswith('/jarvis.js') else None)
                page.on('request', lambda req: requests.append(req.post_data_json) if req.url.endswith('/api/jarvis/turn') else None)
                page.goto('http://127.0.0.1:' + str(server.server_port))
                page.wait_for_function('currentState !== null && jarvis.sessionId !== null')
                self.assertEqual(page.locator('#input-mode').input_value(), 'jarvis')
                page.evaluate("window.syntheticSpeech = []; voice.speakReply = async (text) => { if (typeof text === 'string') syntheticSpeech.push(text); voice.presentReply(text); };")
                for number, text in enumerate(('What is ready?', 'Read Dog Data', request,
                                                'While it runs what did the controller say?')):
                    page.locator('#phone-text').fill(text)
                    page.locator('#phone-input [type=submit]').click()
                    page.wait_for_function('(count) => jarvis.pending.size === 0 && speechRecords.length >= count', arg=number + 1)
                page.evaluate('jarvis.refresh()')
                page.wait_for_function("jarvis.summary.subject === 'Controller'")
                self.assertEqual(len(requests), 4)
                self.assertEqual(contexts[2]['subject'], self.targets['Dog Data'])
                self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])
                self.assertIn(body.encode(), self.received('Dog Data'))
                self.assertEqual(self.received('Dog Data').count(body.encode()), 1)
                originals = page.evaluate('speechRecords.filter(record => record.reply?.target === "Dog Data").map(record => record.reply.text)')
                self.assertIn('Five of six samples passed; one needs review.', originals)
                self.assertIn('Five of six samples passed; one needs review.', page.evaluate('syntheticSpeech'))
                self.assertTrue(all('all done' not in text for text in page.evaluate('syntheticSpeech')))
                self.assertEqual(scripts, [200])
                self.assertEqual(errors, [])
                output = Path(__file__).parent / '.run/jarvis-real-browser'
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                (output / 'browser.json').write_text(json.dumps({'passed': True, 'requests': len(requests),
                    'provider': 'synthetic', 'http_server': 'actual server.make_server',
                    'terminal': 'disposable actual session_actions paste/Enter with synthetic identity/history',
                    'checks': ['jarvis.js served', 'default Jarvis mode', 'ready/read/contextual send/read',
                               'subject independent of visible Notes', 'exact one delivery', 'original preserved',
                               'receipt-derived speech rejects raw done', 'no browser errors'],
                    'scope': 'Actual Chromium page and HTTP; voice speakReply replaced with capture, no microphone/physical phone/live model.'}, indent=2) + '\n')
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
