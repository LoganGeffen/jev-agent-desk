import copy
from contextlib import contextmanager
import unittest
from unittest.mock import patch

import native_edit as edit


def turn(id, text):
    return {'id': id, 'status': 'completed', 'items': [
        {'id': 'item-' + id, 'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}]}


class NativeEditTests(unittest.TestCase):
    def setUp(self):
        self.agent = {'thread_id': 'source', 'process': 'pid:start', 'provider': 'codex'}
        self.source = {'id': 'source', 'historyMode': 'paginated', 'model': 'model',
                       'reasoningEffort': 'low', 'cwd': '/tmp', 'turns': [turn('a', 'First'), turn('b', 'Second')]}
        self.calls = []
        @contextmanager
        def rpc():
            def call(method, params):
                self.calls.append((method, params))
                return {'thread': {'id': 'branch', 'forkedFromId': 'source', 'sessionId': 'family'}}
            yield call
        self.rpc = patch.object(edit.codex_actions, 'app_server', rpc)
        self.rpc.start()
        self.addCleanup(self.rpc.stop)

    def run_edit(self, index=1, reads=None, send_error=None, identity=None):
        reads = reads or [self.source, {'turns': self.source['turns'][:index]}, self.source]
        with patch.object(edit, 'require_empty_composer'), \
             patch.object(edit.codex_actions, 'thread_read', side_effect=reads), \
             patch.object(edit, 'observed_identity', return_value=identity or {'thread_id': 'branch'}), \
             patch.object(edit.codex_actions, 'send_message', side_effect=send_error) as send:
            t = self.source['turns'][index]
            result = edit.edit_message('socket', self.agent, t['id'], t['items'][0]['id'],
                                       t['items'][0]['content'][0]['text'], 'Changed')
            return result, send.call_args_list

    def test_forks_exact_predecessor_and_submits_once_to_verified_branch(self):
        result, sent = self.run_edit()
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.calls[0][1]['lastTurnId'], 'a')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([call.args[2] for call in sent], ['/resume branch', 'Changed'])
        self.assertEqual(sent[-1].args[1]['thread_id'], 'branch')

    def test_first_message_reverts_only_new_branch(self):
        result, _ = self.run_edit(0)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.calls[-1], ('thread/revert', {'threadId': 'branch', 'beforeTurnId': 'a'}))

    def test_changed_source_does_not_activate_branch(self):
        changed = copy.deepcopy(self.source)
        changed['turns'].append(turn('c', 'Later'))
        result, sent = self.run_edit(reads=[self.source, {'turns': [self.source['turns'][0]]}, changed])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['draft'], 'Changed')
        self.assertEqual(sent, [])

    def test_uncertain_resume_never_submits_or_retries(self):
        result, sent = self.run_edit(send_error=TimeoutError('restart failed'))
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(sent), 1)
        self.assertEqual(result['branch_thread_id'], 'branch')
        self.assertEqual(result['draft'], 'Changed')

    def test_replaced_identity_never_submits(self):
        result, sent = self.run_edit(identity={'thread_id': 'other'})
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(sent), 1)

    def test_stale_turn_and_attachments_rejected(self):
        with self.assertRaises(ValueError):
            edit.edit_target(self.source, 'b', 'item-b', 'Old')
        self.source['turns'][1]['items'][0]['content'].append({'type': 'image'})
        with self.assertRaises(ValueError):
            edit.edit_target(self.source, 'b', 'item-b', 'Second')

    def test_empty_native_composer_requires_dim_placeholder(self):
        placeholder = '\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n  Model\n'
        with patch.object(edit.codex_actions, 'check_target'), \
             patch.object(edit.codex_actions, 'tmux', return_value=placeholder):
            edit.require_empty_composer('socket', {'pane_id': '%1'})
        for screen in ('› Draft\n\n Model', '› \n\n  Draft\n', placeholder + '• Working (2s • esc to interrupt)'):
            with patch.object(edit.codex_actions, 'check_target'), \
                 patch.object(edit.codex_actions, 'tmux', return_value=screen):
                with self.assertRaises(ValueError):
                    edit.require_empty_composer('socket', {'pane_id': '%1'})


if __name__ == '__main__':
    unittest.main()
