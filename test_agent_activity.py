from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_activity import AgentActivity


SID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def event(kind, turn='turn-a', second=10, **payload):
    return json.dumps({'timestamp': f'2026-10-07T05:00:{second:02d}Z', 'type': 'event_msg',
                       'payload': {'type': kind, 'turn_id': turn, **payload}}) + '\n'


class CodexActivityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / ('rollout-' + SID + '.jsonl')
        self.path.touch()
        self.database = self.root / 'state_5.sqlite'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
            connection.execute('INSERT INTO threads VALUES (?,?)', (SID, str(self.path)))
        self.reader = AgentActivity(self.root)
        self.agent = {'provider': 'codex', 'thread_id': SID, 'process': '100:200'}
        patcher = patch('agent_activity.process_started',
                        return_value=datetime(2026, 10, 7, 4, tzinfo=timezone.utc))
        patcher.start()
        self.addCleanup(patcher.stop)

    def append(self, text):
        with self.path.open('a') as stream:
            stream.write(text)

    def read(self):
        return self.reader.read(self.agent)

    def test_native_start_ignores_reconstructed_interruption_and_has_no_silence_timeout(self):
        self.append(event('task_started'))
        history = {'status': {'type': 'notLoaded'}, 'turns': [{'status': 'interrupted'}]}
        self.assertEqual(self.reader.read(self.agent, history)['state'], 'working')
        self.assertEqual(self.read()['state'], 'working')
        self.assertNotIn('items', repr(self.reader.cache))

    def test_completed_reply_new_turn_and_late_old_close(self):
        self.append(event('task_started'))
        self.append(event('task_complete', second=11, last_agent_message='PRIVATE SYNTHETIC REPLY'))
        self.assertEqual(self.read()['state'], 'ready')
        self.assertNotIn('PRIVATE SYNTHETIC REPLY', repr(self.reader.cache))
        self.append(event('task_started', 'turn-b', 12))
        self.append(event('task_complete', 'turn-a', 13, last_agent_message='Old'))
        state = self.read()
        self.assertEqual((state['state'], state['turn_id']), ('working', 'turn-b'))
        self.append(event('turn_aborted', 'turn-b', 14, reason='interrupted'))
        self.assertEqual(self.read()['state'], 'stopped')

    def test_complete_without_reply_is_idle_and_start_can_be_outside_initial_tail(self):
        self.append(event('task_complete', last_agent_message='  '))
        self.assertEqual(self.read()['state'], 'idle')
        fresh = AgentActivity(self.root)
        self.assertEqual(fresh.read(self.agent)['state'], 'idle')

    def test_partial_close_is_unknown_then_recovers_when_same_marker_is_complete(self):
        self.append(event('task_started'))
        self.assertEqual(self.read()['state'], 'working')
        closing = event('task_complete', second=11, last_agent_message='Reply')
        self.append(closing[:25])
        self.assertEqual(self.read()['state'], 'unknown')
        self.append(closing[25:])
        self.assertEqual(self.read()['state'], 'ready')

    def test_malformed_row_requires_new_marker_but_completed_partial_row_restores_evidence(self):
        self.append(event('task_started'))
        self.assertEqual(self.read()['state'], 'working')
        self.append('{bad\n')
        self.assertEqual(self.read()['state'], 'unknown')
        self.append(json.dumps({'type': 'response_item', 'payload': {'text': 'private'}}) + '\n')
        self.assertEqual(self.read()['state'], 'unknown')
        self.append(event('task_started', 'turn-b', 12))
        self.assertEqual(self.read()['state'], 'working')
        self.append('{"type":"res')
        self.assertEqual(self.read()['state'], 'unknown')
        self.append('ponse_item"}\n')
        self.assertEqual(self.read()['state'], 'working')
        self.append(event('task_complete', 'turn-b', 14, last_agent_message='Reply'))
        self.assertEqual(self.read()['state'], 'ready')

    def test_truncation_and_atomic_file_replacement_invalidate_cached_status(self):
        self.append(event('task_started'))
        original = self.read()
        self.assertEqual(original['state'], 'working')
        self.path.write_text('{}\n')
        truncated = self.read()
        self.assertEqual(truncated['state'], 'unknown')
        self.assertNotEqual(original['generation'], truncated['generation'])
        replacement = self.root / 'replacement'
        replacement.write_text(event('task_complete', 'new', 11, last_agent_message='New'))
        replacement.replace(self.path)
        state = self.read()
        self.assertEqual((state['state'], state['turn_id']), ('ready', 'new'))
        self.assertNotEqual(truncated['generation'], state['generation'])
        self.assertEqual(state['generation'], self.read()['generation'])

    def test_tail_without_lifecycle_is_unknown_and_recovers_incrementally(self):
        self.append(event('task_started'))
        self.append(json.dumps({'type': 'response_item', 'payload': {'text': 'x' * 2000}}) + '\n')
        with patch('agent_activity.READ_LIMIT', 512):
            self.assertEqual(self.read()['state'], 'unknown')
            self.append(event('task_complete', second=11, last_agent_message='Reply'))
            self.assertEqual(self.read()['state'], 'ready')

    def test_unclosed_start_from_previous_process_is_unknown(self):
        self.append(event('task_started'))
        with patch('agent_activity.process_started',
                   return_value=datetime(2026, 10, 7, 5, 1, tzinfo=timezone.utc)):
            self.assertEqual(self.read()['state'], 'unknown')

    def test_real_writer_child_lifetime_overrides_persistent_pane_shell_lifetime(self):
        self.append(event('task_started'))
        process = self.root / '999'
        process.mkdir()
        (process / 'comm').write_text('codex\n')
        (process / 'stat').write_text('999 (codex) ' + ' '.join(['0'] * 19 + ['100']))
        old_shell = datetime(2026, 10, 7, 4, tzinfo=timezone.utc)
        new_writer = datetime(2026, 10, 7, 5, 1, tzinfo=timezone.utc)
        with patch('agent_activity.process_tree', return_value=[process]), \
             patch('agent_activity.writer_thread_ids', return_value={SID}), \
             patch('agent_activity.process_started', side_effect=lambda identity: new_writer if identity == '999:100' else old_shell):
            self.assertEqual(self.read()['state'], 'unknown')

    def test_invalid_identity_external_path_and_missing_metadata_fail_closed(self):
        self.assertEqual(self.reader.read({**self.agent, 'thread_id': 'not-an-id'})['state'], 'unknown')
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('UPDATE threads SET rollout_path=?', ('/tmp/rollout-' + SID + '.jsonl',))
        self.assertEqual(self.read()['state'], 'unknown')
        self.database.unlink()
        self.assertEqual(self.read()['state'], 'unknown')

    def test_newest_numeric_database_is_authoritative(self):
        with closing(sqlite3.connect(self.root / 'state_12.sqlite')) as connection, connection:
            connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
        self.append(event('task_complete', last_agent_message='Old database'))
        self.assertEqual(self.read()['state'], 'unknown')


class ClaudeActivityTests(unittest.TestCase):
    def setUp(self):
        self.reader = AgentActivity('/fixture')
        self.agent = {'provider': 'claude', 'thread_id': SID, 'process': '100:200', 'run_dir': '/fixture'}
        self.binding = {'session_id': SID, 'process': '100:200', 'event': 'Stop',
                        'observed_at': '2026-10-07T05:00:20Z', 'last_reply': 'Reply'}
        patcher = patch('agent_activity.read_binding', side_effect=lambda *args: self.binding)
        patcher.start()
        self.addCleanup(patcher.stop)

    def thread(self, status='completed', start='2026-10-07T05:00:10Z', end='2026-10-07T05:00:19Z', text='Reply'):
        return {'id': SID, 'turns': [{'id': 'turn-a', 'startedAt': start, 'completedAt': end,
                 'status': status, 'items': [{'type': 'agentMessage', 'phase': 'final_answer', 'text': text}]}]}

    def test_current_stop_requires_matching_latest_final(self):
        self.assertEqual(self.reader.read(self.agent, self.thread())['state'], 'ready')
        self.assertEqual(self.reader.read(self.agent, self.thread(text='Different'))['state'], 'unknown')
        self.assertEqual(self.reader.read(self.agent, self.thread(start=None))['state'], 'unknown')

    def test_old_stop_cannot_validate_same_turn_repeated_reply_after_continuation(self):
        for end in ('2026-10-07T05:00:21Z', None):
            self.assertEqual(self.reader.read(self.agent, self.thread(end=end))['state'], 'unknown')

    def test_new_prompt_keeps_working_despite_old_reply(self):
        self.binding.update(event='UserPromptSubmit')
        self.assertEqual(self.reader.read(self.agent, self.thread())['state'], 'working')

    def test_stop_failure_is_failed_and_newer_interrupt_is_stopped(self):
        self.binding.update(event='StopFailure')
        self.assertEqual(self.reader.read(self.agent, self.thread())['state'], 'failed')
        self.binding.update(event='UserPromptSubmit')
        self.assertEqual(self.reader.read(self.agent, self.thread(status='interrupted',
                         end='2026-10-07T05:00:21Z'))['state'], 'stopped')

    def test_old_stop_cannot_override_newer_transcript_turn(self):
        thread = self.thread(status='inProgress', start='2026-10-07T05:00:21Z', end=None)
        self.assertEqual(self.reader.read(self.agent, thread)['state'], 'working')
        thread['turns'][0]['status'] = 'completed'
        self.assertEqual(self.reader.read(self.agent, thread)['state'], 'unknown')

    def test_missing_mismatched_or_unavailable_binding_and_history_are_unknown(self):
        self.binding['session_id'] = OTHER
        self.assertEqual(self.reader.read(self.agent, self.thread())['state'], 'unknown')
        self.binding['session_id'] = SID
        self.assertEqual(self.reader.read(self.agent, {'history_missing': True})['state'], 'unknown')
        with patch('agent_activity.claude_actions.thread_read', side_effect=OSError('private failure')):
            result = self.reader.read(self.agent)
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('private failure', repr(result))

    def test_new_session_is_idle_when_no_current_transcript_turn(self):
        self.binding.update(event='SessionStart')
        self.assertEqual(self.reader.read(self.agent, {'id': SID, 'turns': []})['state'], 'idle')


if __name__ == '__main__':
    unittest.main()
