import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_actions
from pane_identity import process_identity
from server import tmux
import test_playground
from test_playground import answer


class PaneTests(unittest.TestCase):
    setUp = test_playground.PlaygroundTests.setUp

    def test_terminal_includes_scrollback_without_entering_copy_mode(self):
        window = self.tabs[0]['id']
        pane = self.app.panes(window)[0]['id']
        tmux(self.socket, 'respawn-pane', '-k', '-t', pane,
             "printf 'SCROLL_START\\n'; seq 1 100; printf 'SCROLL_END\\n'; sleep 60")
        deadline = time.monotonic() + 5
        while 'SCROLL_END' not in tmux(self.socket, 'capture-pane', '-p', '-t', pane):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.05)
        self.assertNotIn('SCROLL_START', tmux(self.socket, 'capture-pane', '-p', '-t', pane))
        self.app.select_tab(window, pane)
        state = self.app.observe()
        self.assertIn('SCROLL_START', state['terminal_ansi'])
        self.assertIn('SCROLL_END', state['terminal_ansi'])
        self.assertEqual(tmux(self.socket, 'display-message', '-p', '-t', pane,
                              '#{pane_in_mode}'), '0')

    def split(self, window):
        return tmux(self.socket, 'split-window', '-h', '-d', '-P', '-F', '#{pane_id}',
                    '-t', window, 'cat -v')

    def test_split_handoff_discovered_and_renumbered_after_close(self):
        window = self.tabs[0]['id']
        first = self.app.panes(window)[0]['id']
        second = self.split(window)
        for pane, role, peer in ((first, 'source', second), (second, 'successor', first)):
            for key, value in (('id', 'test-handoff'), ('role', role), ('peer', peer)):
                tmux(self.socket, 'set-option', '-p', '-t', pane, '@agent_handoff_' + key, value)
        state = self.app.observe()
        panes = next(tab for tab in state['tabs'] if tab['id'] == window)['panes']
        self.assertEqual([pane['number'] for pane in panes], [1, 2])
        self.assertEqual(panes[1]['handoff'], {'id': 'test-handoff', 'role': 'successor', 'peer': first})
        self.response = answer('select_tab', window, second)
        event = self.app.submit('Go to Alpha pane 2')
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(event['after']['selected_pane'], second)
        self.assertEqual(event['input']['state']['tabs'][0]['panes'][1]['handoff']['role'], 'successor')
        self.response = answer('close_pane', window, first)
        event = self.app.submit('Close Alpha pane 1')
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(len(self.app.panes(window)), 2)
        self.app.confirm_close(self.app.pending_close['id'])
        remaining = self.app.panes(window)
        self.assertEqual([(p['id'], p['number']) for p in remaining], [(second, 1)])
        self.assertEqual(event['target_label'], 'Alpha · pane 1')

    def test_input_requires_exact_active_pane_and_identity(self):
        window = self.tabs[0]['id']
        second = self.split(window)
        self.app.select_tab(window, second)
        pane = next(p for p in self.app.panes(window) if p['id'] == second)
        self.app.terminal_input(window, text='ONLY_SECOND', pane_id=second, identity=pane['identity'])
        time.sleep(.05)
        self.assertIn('ONLY_SECOND', tmux(self.socket, 'capture-pane', '-p', '-t', second))
        self.assertNotIn('ONLY_SECOND', tmux(self.socket, 'capture-pane', '-p', '-t', '%0'))
        with self.assertRaisesRegex(ValueError, 'explicit pane'):
            self.app.terminal_input(window, key='Escape')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.terminal_input(window, key='Escape', pane_id=second, identity='stale')
        self.app.select_tab(window, '%0')
        with self.assertRaisesRegex(ValueError, 'no longer selected'):
            self.app.terminal_input(window, key='Escape', pane_id=second, identity=pane['identity'])

    def test_pane_moved_to_other_tab_cannot_receive_old_action(self):
        window = self.tabs[0]['id']
        second = self.split(window)
        identity = self.app.panes(window)[1]['identity']
        tmux(self.socket, 'join-pane', '-d', '-s', second, '-t', self.tabs[1]['id'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.close_pane(window, second, identity)
        self.assertIn(second, [pane['id'] for pane in self.app.panes(self.tabs[1]['id'])])

    def test_close_tab_checks_membership_and_removes_only_that_tab(self):
        window = self.tabs[0]['id']
        identities = {p['id']: p['identity'] for p in self.app.panes(window)}
        self.split(window)
        with self.assertRaisesRegex(ValueError, 'panes changed'):
            self.app.close_tab(window, identities)
        self.response = answer('close_tab', window)
        event = self.app.submit('Close tab Alpha')
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(len(event['after']['tabs']), 4)
        state = self.app.confirm_close(self.app.pending_close['id'])
        self.assertEqual([t['id'] for t in state['tabs']], [t['id'] for t in self.tabs[1:]])

    def test_close_cancel_new_request_and_changed_target_require_new_confirmation(self):
        window = self.tabs[0]['id']
        self.response = answer('close_pane', window, '%0')
        event = self.app.submit('Close Alpha pane 1')
        self.assertTrue(event['confirmation_required'])
        first_id = self.app.pending_close['id']
        self.app.confirm_close(first_id, cancel=True)
        self.assertEqual(len(self.app.observe()['tabs']), 4)

        with self.assertRaisesRegex(ValueError, 'no longer pending'):
            self.app.confirm_close(first_id)
        self.app.submit('Close Alpha pane 1')
        second_id = self.app.pending_close['id']
        self.response = answer('no_action')
        self.app.submit('Never mind')
        self.assertIsNone(self.app.pending_close)
        with self.assertRaises(ValueError):
            self.app.confirm_close(second_id)
        self.response = answer('close_pane', window, '%0')
        self.app.submit('Close Alpha pane 1')
        third_id = self.app.pending_close['id']
        tmux(self.socket, 'respawn-pane', '-k', '-t', '%0', 'cat -v')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.confirm_close(third_id)
        self.assertEqual(len(self.app.observe()['tabs']), 4)

    def test_jev_cannot_propose_closing_ambiguous_codex_conversation(self):
        original = self.app.panes
        def ambiguous(window):
            panes = original(window)
            if window == self.tabs[0]['id']:
                panes[0].update(command='codex', thread_id=None, codex=False)
            return panes
        with patch.object(self.app, 'panes', ambiguous):
            self.response = answer('close_tab', self.tabs[0]['id'])
            event = self.app.submit('Close tab Alpha')
        self.assertEqual(event['outcome'], 'error')
        self.assertIn('identity is unresolved', event['error'])
        self.assertIsNone(self.app.pending_close)

    def test_replaced_pane_during_decision_does_not_close_replacement(self):
        window = self.tabs[0]['id']
        second = self.split(window)
        def decide(payload):
            tmux(self.socket, 'kill-pane', '-t', second)
            self.split(window)
            return answer('close_pane', window, second)
        self.app.evaluate = decide
        event = self.app.submit('Close Alpha pane 2')
        self.assertEqual(event['outcome'], 'error')
        self.assertEqual(len(self.app.panes(window)), 2)

    def test_respawn_during_decision_rejects_same_pane_id(self):
        window = self.tabs[0]['id']
        pane = self.app.panes(window)[0]
        def decide(payload):
            tmux(self.socket, 'respawn-pane', '-k', '-t', pane['id'], 'cat -v')
            return answer('close_pane', window, pane['id'])
        self.app.evaluate = decide
        event = self.app.submit('Close Alpha')
        self.assertEqual(event['outcome'], 'error')
        self.assertEqual(self.app.panes(window)[0]['id'], pane['id'])

    def test_cross_tab_model_answers_fail_without_execution(self):
        self.response = answer('close_pane', self.tabs[0]['id'], self.tabs[1]['panes'][0]['id'])
        event = self.app.submit('Close Alpha pane 1')
        self.assertEqual(event['outcome'], 'error')
        self.assertEqual(len(event['after']['tabs']), 4)

    def test_last_tab_is_kept_open(self):
        for tab in self.tabs[1:]:
            tmux(self.socket, 'kill-window', '-t', tab['id'])
        pane = self.app.panes(self.tabs[0]['id'])[0]
        with self.assertRaisesRegex(ValueError, 'Keep one sandbox tab'):
            self.app.close_pane(self.tabs[0]['id'], pane['id'], pane['identity'])


class IdentityTests(unittest.TestCase):
    def test_live_writer_changes_without_reading_private_files(self):
        with tempfile.TemporaryDirectory(prefix='jev-proc-') as directory:
            root = Path(directory)
            process = root / '42'
            (process / 'task' / '42').mkdir(parents=True)
            (process / 'fd').mkdir()
            (process / 'task' / '42' / 'children').write_text('')
            (process / 'comm').write_text('codex')
            (process / 'stat').write_text('42 (codex) ' + ' '.join(['0'] * 19 + ['123']))
            descriptor = process / 'fd' / '3'
            first = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            second = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
            descriptor.symlink_to(root / 'thread-writer-locks' / (first + '.lock'))
            self.assertEqual(process_identity(42, root, codex_home=root)['thread_id'], first)
            descriptor.unlink()
            descriptor.symlink_to(root / 'thread-writer-locks' / (second + '.lock'))
            self.assertEqual(process_identity(42, root, codex_home=root)['thread_id'], second)
            (process / 'fd' / '4').symlink_to(root / 'thread-writer-locks' / (first + '.lock'))
            self.assertIsNone(process_identity(42, root, codex_home=root)['thread_id'])
            self.assertEqual(process_identity(42, root, codex_home=root, title=second)['thread_id'], second)
            self.assertEqual(process_identity(42, root, codex_home=root, title=first[:25] + '...')['thread_id'], first)
            self.assertEqual(process_identity(42, root, codex_home=root, title=second[:25] + '... ⠸')['thread_id'], second)
            self.assertIsNone(process_identity(42, root, codex_home=root, title='cccccccc-cccc-cccc-cccc-cccccccccccc')['thread_id'])

    def test_stale_thread_cannot_read_or_send(self):
        agent = {'socket': 'test', 'window_id': '@1', 'pane_id': '%1', 'thread_id': 'old'}
        with patch.object(codex_actions, 'tmux', side_effect=['@1\tcodex', '42\ttitle']), \
             patch.object(codex_actions, 'process_identity', return_value={'thread_id': 'new'}), \
             patch.object(codex_actions, 'thread_read') as history:
            with self.assertRaisesRegex(RuntimeError, 'conversation changed'):
                codex_actions.read_reply('test', agent)
            history.assert_not_called()


if __name__ == '__main__':
    unittest.main()
