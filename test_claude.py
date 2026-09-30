import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claude_actions
import claude_runtime
import jev
import session_actions
import test_playground
from codex_actions import latest_reply
from server import tmux


SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


class ClaudeHookTests(unittest.TestCase):
    def event(self, kind, session=SID, **values):
        return {'hook_event_name': kind, 'session_id': session, 'transcript_path': '/not-read', **values}

    def test_clear_and_resume_replace_identity_without_carrying_old_reply(self):
        start = claude_runtime.apply_event(None, self.event('SessionStart'), '1:2')
        stop = claude_runtime.apply_event(start, self.event('Stop', last_assistant_message='old'), '1:2')
        cleared = claude_runtime.apply_event(stop, self.event('SessionStart', OTHER, source='clear'), '1:2')
        self.assertEqual(cleared['session_id'], OTHER)
        self.assertNotIn('last_reply', cleared)
        resumed = claude_runtime.apply_event(stop, self.event('SessionStart', source='resume'), '1:2')
        self.assertNotIn('last_reply', resumed)

    def test_late_old_session_and_subagent_hooks_cannot_replace_current_binding(self):
        current = claude_runtime.apply_event(None, self.event('SessionStart', OTHER), '1:2')
        for event in (self.event('SessionEnd'), self.event('Stop', last_assistant_message='stale'),
                      self.event('UserPromptSubmit'), self.event('Stop', OTHER, agent_id='subagent')):
            self.assertEqual(claude_runtime.apply_event(current, event, '1:2'), current)

    def test_session_end_invalidates_binding(self):
        with tempfile.TemporaryDirectory() as root:
            event = claude_runtime.apply_event(None, self.event('SessionEnd'), '1:2')
            path = claude_runtime.binding_path(root, '1:2')
            path.parent.mkdir()
            path.write_text(json.dumps(event))
            self.assertIsNone(claude_runtime.read_binding(root, '1:2'))
            self.assertIsNone(claude_runtime.read_binding(root, '2:3'))


class ClaudeHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / '.claude/projects/test' / (SID + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.binding = {'session_id': SID, 'transcript_path': str(self.path)}
        patcher = patch('claude_actions.Path.home', return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_transcript_is_not_fabricated_reply(self):
        self.assertTrue(claude_actions.thread_read(self.binding)['history_missing'])

    def test_only_selected_parent_chain_and_final_text_are_read(self):
        rows = [
            {'uuid': 'u', 'parentUuid': None, 'type': 'user', 'message': {'content': 'question'}},
            {'uuid': 'discarded', 'parentUuid': 'u', 'type': 'assistant', 'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'wrong branch'}]}},
            {'uuid': 'a', 'parentUuid': 'u', 'type': 'assistant', 'timestamp': 'now', 'message': {'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'not speech'}, {'type': 'text', 'text': 'answer'}]}},
            {'uuid': 'sub', 'parentUuid': 'a', 'isSidechain': True, 'type': 'assistant', 'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'subagent'}]}},
        ]
        self.path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n{"partial":')
        history = claude_actions.thread_read(self.binding)
        self.assertEqual(latest_reply(history), 'answer')
        self.assertNotIn('wrong branch', json.dumps(history))
        self.assertNotIn('not speech', json.dumps(history))

    def test_transcript_path_cannot_read_arbitrary_file(self):
        for path in (self.root / 'secret.json', self.root / '.claude/projects/test/other.jsonl'):
            with self.assertRaises(ValueError):
                claude_actions.thread_read({**self.binding, 'transcript_path': str(path)})


class ClaudePaneTests(unittest.TestCase):
    setUp = test_playground.PlaygroundTests.setUp

    def fake_claude(self):
        window, pane = '@0', '%0'
        original = self.app.panes
        def panes(tab):
            values = original(tab)
            if tab == window:
                values[0].update(agent=True, codex=False, provider='claude', thread_id=SID, command='claude')
            return values
        patcher = patch.object(self.app, 'panes', panes)
        patcher.start()
        self.addCleanup(patcher.stop)
        return window, pane

    def test_claude_routing_and_dispatch_do_not_require_codex_flag(self):
        window, pane = self.fake_claude()
        self.response = test_playground.answer('read_reply', window, pane)
        with patch('claude_actions.read_reply', return_value='CLAUDE_REPLY') as read:
            event = self.app.submit('Read Alpha')
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(event['output'], 'CLAUDE_REPLY')
        self.assertEqual(read.call_args.args[1]['provider'], 'claude')
        tab = next(tab for tab in event['input']['state']['tabs'] if tab['id'] == window)
        self.assertTrue(tab['agent'])
        self.assertFalse(tab['codex'])
        self.assertEqual(tab['panes'][0]['provider'], 'claude')

    def test_stale_process_or_session_prevents_claude_send(self):
        agent = {'socket': self.socket, 'window_id': '@0', 'pane_id': '%0', 'process': '10:20',
                 'thread_id': SID, 'run_dir': str(self.root), 'provider': 'claude'}
        with patch('claude_actions.tmux', return_value='@0\tclaude\t10') as commands, \
             patch('claude_actions.process_identity', return_value={'process': '10:21'}), \
             patch('claude_actions.read_binding', return_value={'session_id': SID}):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                session_actions.send_message(self.socket, agent, 'must not send')
            self.assertEqual(commands.call_count, 1)

    def test_claude_in_explicit_envelope_has_send_but_no_shutdown_action(self):
        self.fake_claude()
        state = self.app.observe()
        payload = jev.question('Tell Alpha: shutdown Beta', state['tabs'], '@0')
        self.assertTrue(next(tab for tab in payload['state']['tabs'] if tab['id'] == '@0')['agent'])
        self.assertEqual(set(payload['questions']['action']['criteria']), {'send_message', 'no_action'})


if __name__ == '__main__':
    unittest.main()
