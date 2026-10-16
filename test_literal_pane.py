from unittest.mock import Mock, patch

from test_jarvis_tools import DisposableDesk


class LiteralPaneTests(DisposableDesk):
    def setUp(self):
        super().setUp()
        self.target = {key: self.targets['Dog Data'][key] for key in ('tab', 'pane', 'identity')}
        self.app.evaluate = Mock(side_effect=AssertionError('No interpretation is allowed'))

    def test_question_command_output_and_routing_words_are_exact_input(self):
        self.app.focus(self.targets['Notes']['tab'])
        messages = [
            '  (base) > elembio login --profile example\nSuccessfully authenticated!\n\n',
            'Okay so whats on my list',
            'What specifically are they doing? Are they running phase B work?',
            'Why are so many AWS CPUs occupied?',
            'Tell Controller: keep all of these words. Café 🐕',
            '/status',
        ]
        for number, text in enumerate(messages):
            with self.subTest(text=text):
                result = self.app.submit_literal(text, request_id=str(number), capture_target=self.target)
                self.assertEqual(result['delivery'], 'submitted', result)
                self.assertEqual(result['execution']['text'], text)
                self.assertEqual(result['target'], self.target['tab'])
                self.assertIn(text.replace('\n', '\r').encode(), self.received('Dog Data'))
        self.app.evaluate.assert_not_called()
        self.assertEqual(self.received('Controller'), b'')
        self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])

    def test_browser_pane_mode_sends_the_captured_text_without_interpretation(self):
        import threading
        from playwright.sync_api import expect, sync_playwright
        from server import make_server

        server = make_server(self.app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.goto('http://127.0.0.1:' + str(server.server_port))
            page.wait_for_function('currentState !== null')
            page.evaluate("""async target => {
              deviceView.select(currentState, target.tab, target.pane);
              await refresh(); setInputMode('direct');
            }""", self.target)
            text = '  Okay so whats on my list\nTell Controller: keep this here.  '
            page.locator('#phone-text').fill(text)
            with page.expect_response(lambda response: response.url.endswith('/api/request')) as response:
                page.locator('#phone-input [type=submit]').click()
            receipt = response.value.json()
            self.assertEqual(receipt['outcome'], 'ok', receipt)
            self.assertEqual(receipt['execution']['text'], text)
            self.assertEqual(receipt['target'], self.target['tab'])
            self.assertIn(text.replace('\n', '\r').encode(), self.received('Dog Data'))
            self.assertEqual(self.received('Controller'), b'')
            self.app.evaluate.assert_not_called()
            page.wait_for_function('!submitting')
            page.locator('#phone-text').fill('/status')
            with page.expect_response(lambda response: response.url.endswith('/api/request')) as response:
                page.locator('#phone-input [type=submit]').click()
            self.assertEqual(response.value.json()['delivery'], 'submitted')
            terminal = page.locator('.activity-terminal').locator('..')
            expect(terminal).to_be_visible()
            expect(terminal).to_have_attribute('open', '')
            self.assertIn(b'/status', self.received('Dog Data'))
            self.assertEqual(self.received('Controller'), b'')
            browser.close()

    def test_missing_and_stale_targets_never_fall_back_to_selected_pane(self):
        with patch('server.send_message') as send:
            for target in (None, {}, {**self.target, 'identity': ''}):
                with self.assertRaises(ValueError):
                    self.app.submit_literal('text', request_id='missing', capture_target=target)
            result = self.app.submit_literal('text', request_id='stale', capture_target={**self.target, 'identity': 'stale'})
            self.assertEqual(result['delivery'], 'not_attempted')
            self.assertEqual(result['outcome'], 'error')
            send.assert_not_called()
        self.app.evaluate.assert_not_called()

    def test_uncertain_delivery_is_not_replayed(self):
        with patch('server.send_message', side_effect=RuntimeError('Lost delivery response')) as send:
            first = self.app.submit_literal('exact', request_id='once', capture_target=self.target)
            second = self.app.submit_literal('exact', request_id='once', capture_target=self.target)
            self.assertEqual(first, second)
            self.assertEqual(first['delivery'], 'uncertain')
            send.assert_called_once()
            with self.assertRaisesRegex(ValueError, 'different submitted input'):
                self.app.submit_literal('changed', request_id='once', capture_target=self.target)

    def test_pending_interpretation_cannot_consume_literal_input(self):
        self.app.pending_clarification = {'id': 'old', 'request': 'old routing question'}
        result = self.app.submit_literal('yes', request_id='literal-yes', capture_target=self.target)
        self.assertEqual(result['delivery'], 'submitted')
        self.assertIn(b'yes', self.received('Dog Data'))
        self.app.evaluate.assert_not_called()
