import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pane_identity import process_identity


ROOT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'


class SessionIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc = self.root / 'proc'
        self.home = self.root / 'provider'
        self.home.mkdir()
        process = self.proc / '42'
        (process / 'task' / '42').mkdir(parents=True)
        (process / 'fd').mkdir()
        (process / 'task' / '42' / 'children').write_text('')
        (process / 'comm').write_text('codex')
        (process / 'stat').write_text('42 (codex) ' + ' '.join(['0'] * 19 + ['123']))
        for number, thread in enumerate((ROOT, CHILD)):
            (process / 'fd' / str(number)).symlink_to(
                self.home / 'thread-writer-locks' / (thread + '.lock'))
        self.database = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TABLE threads (id, source, model, reasoning_effort, sandbox_policy, approval_mode)')
            for thread, source in ((ROOT, 'cli'), (CHILD, 'subagent')):
                connection.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)',
                                   (thread, source, 'fixture', 'low', '{"type":"read-only"}', 'on-request'))

    def live(self, title=''):
        return process_identity(42, self.proc, title, self.home)


    def test_live_identity_excludes_subagents(self):
        self.assertEqual(self.live()['thread_id'], ROOT)
        self.assertEqual(self.live()['tracking'], 'interactive_metadata')

    def test_multiple_interactive_conversations_are_unresolved(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE threads SET source = 'cli'")
        self.assertIsNone(self.live()['thread_id'])
        self.assertEqual(self.live(CHILD)['thread_id'], CHILD)

    def test_unknown_writer_cannot_be_discarded_as_subagent(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('DELETE FROM threads WHERE id = ?', (CHILD,))
        self.assertIsNone(self.live()['thread_id'])

    def test_missing_metadata_fails_closed_for_multiple_writers(self):
        self.database.unlink()
        self.assertIsNone(self.live()['thread_id'])

    def test_unreadable_metadata_fails_closed(self):
        self.database.write_text('invalid sqlite')
        self.assertIsNone(self.live()['thread_id'])

    def test_title_changes_follow_current_held_conversation(self):
        with patch('pane_identity.thread_metadata') as metadata:
            self.assertEqual(self.live(ROOT)['thread_id'], ROOT)
            self.assertEqual(self.live(CHILD[:25] + '... ⠸')['thread_id'], CHILD)
            self.assertIsNone(self.live('33333333-3333-4333-8333-333333333333')['thread_id'])
            metadata.assert_not_called()

    def test_live_metadata_is_rechecked_after_conversation_change(self):
        self.assertEqual(self.live()['thread_id'], ROOT)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE threads SET source = CASE WHEN id = ? THEN 'cli' ELSE 'subagent' END", (CHILD,))
        self.assertEqual(self.live()['thread_id'], CHILD)

    def test_single_writer_needs_no_metadata_or_restore_settings(self):
        (self.proc / '42' / 'fd' / '1').unlink()
        self.database.unlink()
        self.assertEqual(self.live()['thread_id'], ROOT)
        self.assertEqual(self.live()['tracking'], 'single_writer')


class DaemonIdentityTests(unittest.TestCase):
    live = SessionIdentityTests.live
    def setUp(self):
        SessionIdentityTests.setUp(self)
        self.peer = self.proc / '99'
        self.peer.mkdir()
        (self.proc / '42' / 'fd').rename(self.peer / 'fd')
        (self.proc / '42' / 'fd').mkdir()
        (self.proc / '42' / 'fd' / '0').symlink_to('socket:[100]')
        (self.peer / 'comm').write_text('codex')
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('ALTER TABLE threads ADD COLUMN name')
            connection.execute('UPDATE threads SET name = ?, source = ?', ('Same name', 'vscode'))
        self.sockets = patch('pane_identity.subprocess.run')
        self.run = self.sockets.start()
        self.addCleanup(self.sockets.stop)
        self.run.return_value.stdout = 'u_str ESTAB 0 0 /tmp/codex-daemon-1000/fixture 200 * 100 users:(("codex",pid=99,fd=8))'

    def test_daemon_title_requires_unique_name_and_live_writer(self):
        self.assertIsNone(self.live('Same name | user')['thread_id'])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('UPDATE threads SET name = ? WHERE id = ?', ('Other name', CHILD))
        self.assertEqual(self.live('Same name | user')['thread_id'], ROOT)
        self.assertEqual(self.live('Same name | user')['tracking'], 'daemon_title')
        self.assertEqual(self.live('Other name | user')['thread_id'], CHILD)
        self.assertIsNone(self.live('Same')['thread_id'])
        (self.peer / 'fd' / '0').unlink()
        self.assertIsNone(self.live('Same name | user')['thread_id'])

    def test_unconnected_daemon_cannot_supply_identity(self):
        self.run.return_value.stdout = self.run.return_value.stdout.replace('* 100 ', '* 101 ')
        self.assertIsNone(self.live('Same name | user')['thread_id'])
