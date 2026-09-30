import threading
import unittest
from unittest.mock import patch

import test_playground
import jev
from server import Playground, make_server, tmux
from test_playground import answer


class ClarificationTests(unittest.TestCase):
    setUp = test_playground.PlaygroundTests.setUp

    def proposal(self, request='Luna pane 1, close this', message='close this', scope='pane'):
        window = self.tabs[0]['id']
        self.app.rename_tab(window, 'Luna')
        original = self.app.panes

        def panes(tab):
            records = original(tab)
            if tab == window:
                records[0].update(codex=True, thread_id='test-conversation', command='codex')
            return records

        patcher = patch.object(self.app, 'panes', panes)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.response = answer('clarify_close', window, '%0')
        self.response['answers'].update({
            'close_scope': {'choice': scope},
            'clarification_start': {'choice': str(request.index(message))},
            'clarification_end': {'choice': str(request.index(message) + len(message))},
        })
        event = self.app.submit(request)
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertTrue(event['clarification_required'])
        self.assertIsNone(self.app.pending_close)
        return self.app.pending_clarification

    def test_click_send_preserves_original_message_and_is_one_shot(self):
        pending = self.proposal(message='close this')
        self.assertIn('close this', self.app.latest['output'])
        with patch('server.send_message') as send:
            state = self.app.resolve_clarification(pending['id'], 'send')
            self.assertEqual(send.call_args.args[2], 'close this')
            self.assertEqual(send.call_args.args[1]['pane_id'], '%0')
            self.assertIsNone(state['pending_clarification'])
            self.assertIsNone(state['pending_close'])
            self.assertEqual(len(state['tabs']), 4)
            with self.assertRaisesRegex(ValueError, 'no longer pending'):
                self.app.resolve_clarification(pending['id'], 'send')
            self.assertEqual(send.call_count, 1)

    def test_voice_send_uses_original_not_followup_words(self):
        self.proposal()
        self.response = {'answers': {'resolution': {'choice': 'send'}}}
        with patch('server.send_message') as send:
            event = self.app.submit('Send Luna the message', source='voice')
            self.assertEqual(event['outcome'], 'ok', event.get('error'))
            self.assertEqual(send.call_args.args[2], 'close this')
            self.assertEqual(event['original_request'], 'Luna pane 1, close this')
            self.assertEqual(event['input']['state']['message'], 'close this')

    def test_shutdown_choice_still_requires_confirmation(self):
        pending = self.proposal()
        self.app.resolve_clarification(pending['id'], 'shutdown')
        self.assertEqual(len(self.app.observe()['tabs']), 4)
        self.assertIsNone(self.app.pending_clarification)
        self.assertTrue(self.app.latest['confirmation_required'])
        self.app.confirm_close(self.app.pending_close['id'])
        self.assertEqual(len(self.app.observe()['tabs']), 3)

    def test_unclear_answer_retains_original_then_cancel_discards(self):
        pending = self.proposal()
        self.response = {'answers': {'resolution': {'choice': 'unclear'}}}
        event = self.app.submit('yes', source='voice')
        self.assertTrue(event['clarification_required'])
        self.assertEqual(self.app.pending_clarification, pending)
        self.app.resolve_clarification(pending['id'], 'cancel')
        self.assertIsNone(self.app.pending_clarification)
        self.assertEqual(len(self.app.observe()['tabs']), 4)

    def test_new_request_discards_pending_and_routes_normally(self):
        pending = self.proposal()
        with patch.object(self.app, 'evaluate', side_effect=[
            {'answers': {'resolution': {'choice': 'new_request'}}}, answer('select_tab', '@1', '%1'), answer('select_tab', '@1', '%1')
        ]):
            event = self.app.submit('Go to Beta')
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(event['after']['selected'], '@1')
        self.assertIsNone(self.app.pending_clarification)
        with self.assertRaises(ValueError):
            self.app.resolve_clarification(pending['id'], 'shutdown')

    def test_replacement_cannot_receive_preserved_message(self):
        pending = self.proposal()
        tmux(self.socket, 'respawn-pane', '-k', '-t', '%0', 'cat -v')
        with patch('server.send_message') as send:
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.app.resolve_clarification(pending['id'], 'send')
            send.assert_not_called()
        self.assertIsNone(self.app.pending_clarification)

    def test_whole_tab_change_invalidates_shutdown_choice(self):
        pending = self.proposal('Close Luna', 'Close', scope='tab')
        tmux(self.socket, 'split-window', '-h', '-d', '-t', '%0', 'cat -v')
        with self.assertRaisesRegex(ValueError, 'panes changed'):
            self.app.resolve_clarification(pending['id'], 'shutdown')
        self.assertIsNone(self.app.pending_close)
        self.assertEqual(len(self.app.panes('@0')), 2)

    def test_restart_does_not_restore_actionable_pending_question(self):
        self.proposal()
        other = Playground(self.socket, self.app.log, self.app.evaluate)
        self.assertTrue(other.latest['clarification_required'])
        self.assertIsNone(other.pending_clarification)

    def test_explicit_message_envelope_cannot_authorize_shutdown(self):
        for request in ('Tell Alpha pane 1: close tab Beta', 'Message Alpha: shutdown Beta'):
            payload = jev.question(request, self.tabs, '@0')
            self.assertEqual(set(payload['questions']['action']['criteria']), {'send_message', 'no_action'})
            self.assertEqual(payload['state']['message_envelope']['recipient_tab'], '@0')
            self.response = answer('close_tab', '@1')
            event = self.app.submit(request)
            self.assertEqual(event['outcome'], 'error')
            self.assertIsNone(self.app.pending_close)
            self.assertEqual(len(self.app.observe()['tabs']), 4)

    def test_clarification_endpoint_preserves_text(self):
        import json
        from urllib.request import Request, urlopen

        pending = self.proposal()
        server = make_server(self.app)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        request = Request(f'http://127.0.0.1:{server.server_port}/api/clarification',
                          data=json.dumps({'id': pending['id'], 'choice': 'send'}).encode(),
                          headers={'Content-Type': 'application/json'})
        with patch('server.send_message') as send, urlopen(request) as response:
            state = json.load(response)
            self.assertEqual(send.call_args.args[2], 'close this')
            self.assertEqual(state['latest']['action'], 'send_message')


if __name__ == '__main__':
    unittest.main()
