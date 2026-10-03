import json
import socket
import threading
import unittest
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright
from server import make_server
from test_jarvis_tools import DisposableDesk, final

STUB = """(() => {
  window.__speech = [];
  const timer = setInterval(() => {
    if (typeof VoiceControls === 'undefined') return;
    clearInterval(timer);
    VoiceControls.prototype.speak = async function(text) { __speech.push({kind: 'speak', text}); };
    VoiceControls.prototype.speakReply = async function(text) { __speech.push({kind: 'reply', text}); this.presentReply(text); };
    window.__speechReady = true;
  }, 0);
})();"""
INTERRUPTED = 'Controller inference interrupted by restart. No action will be retried.'
SUBMITTED = 'Input submitted to Dog Data. Its task is not complete; no automatic retry.'


class RestartBrowserTests(DisposableDesk):
    request, body = 'Tell Dog Data: Café 🐕. Exact input.', 'Café 🐕. Exact input.'

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.parked = threading.Event()
        self.provider_calls = []
        self.accepted = []
        self.serving = None
        self.addCleanup(self.stop_server)
        self.allow(self.request, body=self.body)

    def provider_that_parks(self, send):
        def provider(context, specs, execute, **kwargs):
            self.provider_calls.append(context['current_turn']['user_text'])
            if send:
                draft = execute('prepare_literal', {})
                execute('send_draft', {'draft_id': draft['draft_id']})
            self.parked.set()
            self.release.wait(20)
            return final('Untrusted model: sent and all done.')
        return provider

    def start_server(self, port=0):
        server = make_server(self.app, port)
        server.handle_error = lambda request, address: None
        original = server.get_request
        def tracked():
            connection, address = original()
            self.accepted.append(connection)
            return connection, address
        server.get_request = tracked
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.serving = (server, worker)
        return server.server_port

    def stop_server(self):
        if not self.serving:
            return
        server, worker = self.serving
        self.serving = None
        server.shutdown()
        server.server_close()
        worker.join(2)
        for connection in self.accepted:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.accepted.clear()

    def restart(self, port):
        self.stop_server()
        self.reopen()
        self.provider = lambda *args, **kwargs: self.fail('Recovery must not rerun inference')
        self.provider_calls.clear()
        self.assertEqual(self.start_server(port), port)

    def open_page(self, browser, port, requests):
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.add_init_script(STUB)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda req: requests.append(req.url.rsplit('/', 1)[-1]) if '/api/jarvis/' in req.url and req.method == 'POST' else None)
        page.goto('http://127.0.0.1:' + str(port))
        page.wait_for_function('window.__speechReady === true && currentState !== null && jarvis.sessionId !== null')
        return page, errors

    def submit_and_park(self, page, send=True):
        self.provider = self.provider_that_parks(send)
        page.locator('#phone-text').fill(self.request)
        page.locator('#phone-input [type=submit]').click()
        self.assertTrue(self.parked.wait(5))
        page.wait_for_function('jarvis.pending.size === 1')
        self.assertEqual(self.provider_calls, [self.request])

    def assert_nothing_redispatched(self, requests, sent=1):
        terminal = self.received('Dog Data')
        self.assertEqual(terminal.count(self.body.encode()), sent)
        self.assertEqual(requests.count('turn'), 1)
        self.assertEqual(self.provider_calls, [])

    def quiet_for_polls(self, page):
        page.wait_for_timeout(1600)

    def turn(self):
        return next(iter(self.app.jarvis.snapshot()['turns'].values()))

    def test_current_pending_client_hears_canonical_submitted_status_once(self):
        requests = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                port = self.start_server()
                page, errors = self.open_page(browser, port, requests)
                self.submit_and_park(page)
                self.assertEqual(self.received('Dog Data').count(self.body.encode()), 1)
                self.restart(port)
                page.wait_for_function('__speech.length >= 1 && jarvis.pending.size === 0')
                self.quiet_for_polls(page)
                turn = self.turn()
                self.assertEqual((turn['status'], turn['interrupted']), ('cancelled', True))
                self.assertEqual(turn['result']['text'], SUBMITTED)
                self.assertEqual(page.evaluate('__speech'), [{'kind': 'speak', 'text': SUBMITTED}])
                self.assertIn('Submitted to Dog Data. Submitted is not completed.', page.locator('body').text_content())
                self.assert_nothing_redispatched(requests)
                action = next(iter(self.app.jarvis.snapshot()['actions'].values()))
                self.assertEqual((action['status'], action['payload']), ('submitted', self.body))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_current_pending_client_hears_interruption_when_nothing_was_sent(self):
        requests = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                port = self.start_server()
                page, errors = self.open_page(browser, port, requests)
                self.submit_and_park(page, send=False)
                self.restart(port)
                page.wait_for_function('__speech.length >= 1 && jarvis.pending.size === 0')
                self.quiet_for_polls(page)
                self.assertEqual(page.evaluate('__speech'), [{'kind': 'speak', 'text': INTERRUPTED}])
                self.assert_nothing_redispatched(requests, sent=0)
                self.assertEqual(self.app.jarvis.snapshot()['actions'], {})
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_reloaded_page_stays_quiet_about_the_interrupted_request(self):
        requests = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                port = self.start_server()
                page, errors = self.open_page(browser, port, requests)
                self.submit_and_park(page)
                page.reload()
                page.wait_for_function('window.__speechReady === true && jarvis.sessionId !== null')
                self.restart(port)
                page.wait_for_function('Object.values(jarvis.snapshot?.turns || {}).some(turn => turn.interrupted)')
                self.quiet_for_polls(page)
                self.assertEqual(self.turn()['result']['text'], SUBMITTED)
                self.assertEqual(page.evaluate('__speech'), [])
                self.assertEqual(page.evaluate('jarvis.pending.size'), 0)
                self.assertIn('Superseded or cancelled', page.locator('body').text_content())
                self.assert_nothing_redispatched(requests)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def leave_current_during_outage(self, leave):
        requests = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                port = self.start_server()
                page, errors = self.open_page(browser, port, requests)
                self.submit_and_park(page)
                self.stop_server()
                page.evaluate(leave)
                self.reopen()
                self.provider = lambda *args, **kwargs: self.fail('Recovery must not rerun inference')
                self.provider_calls.clear()
                self.start_server(port)
                page.wait_for_function('jarvis.pending.size === 0')
                self.quiet_for_polls(page)
                self.assertEqual(self.turn()['result']['text'], SUBMITTED)
                self.assertEqual(page.evaluate('__speech'), [])
                self.assert_nothing_redispatched(requests)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_request_superseded_by_direct_mode_stays_quiet_across_restart(self):
        self.leave_current_during_outage('jarvis.supersedeForDirect()')

    def test_request_after_stop_readback_stays_quiet_across_restart(self):
        self.leave_current_during_outage("voice.stopReadback('test stop')")

    def test_explicit_cancellation_of_the_current_request_stays_quiet(self):
        requests = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                port = self.start_server()
                page, errors = self.open_page(browser, port, requests)
                self.submit_and_park(page)
                origin = 'http://127.0.0.1:' + str(port)
                payload = {'session_id': self.app.jarvis.state.session_id, 'request_id': next(iter(self.app.jarvis.snapshot()['requests']))}
                with urlopen(Request(origin + '/api/jarvis/cancel', data=json.dumps(payload).encode(),
                             headers={'Content-Type': 'application/json', 'Origin': origin}), timeout=5) as response:
                    self.assertEqual(json.load(response)['status'], 'cancelled')
                page.wait_for_function('jarvis.pending.size === 0')
                self.quiet_for_polls(page)
                self.assertNotIn('interrupted', self.turn())
                self.assertEqual(page.evaluate('__speech'), [])
                self.assertEqual(self.received('Dog Data').count(self.body.encode()), 1)
                self.assertEqual(requests.count('turn'), 1)
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
