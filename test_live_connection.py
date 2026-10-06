import io
import json
import os
import unittest
from unittest.mock import patch

import desk_mcp
import live_connection
from managed_provider import read_record, record_path
from test_visual_workspace import VisualTests


class LiveConnectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = VisualTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.thread = '11111111-1111-1111-1111-111111111111'
        self.pane = self.fixture.panes['%1']
        self.pane.update(thread_id=self.thread, workspace=str(self.root), id='%1')
        self.native = '500:600'
        self.stamps = {'100': '100:200', '500': '500:600', '900': '900:1000'}
        self.fixture.app.validate_pane = lambda tab, pane, identity=None: self.fixture.validate_pane(tab, pane, identity or self.fixture.panes[pane]['identity'])
        self.fixture.app.state = lambda *args: {'tabs': [{'id': '@1', 'panes': [self.pane.copy()]}]}
        for name, replacement in (
                ('visual_workspace.agent_process', lambda pane: self.native),
                ('live_connection.agent_process', lambda pane: self.native),
                ('managed_provider.process_stamp', lambda pid: self.stamps[str(pid)]),
                ('live_connection.process_stamp', lambda pid: self.stamps[str(pid)]),
                ('live_connection.native_host', lambda process: '900:1000'),
                ('live_connection.parent_processes', lambda: iter(['900:1000']))):
            mock = patch(name, side_effect=replacement)
            mock.start()
            self.addCleanup(mock.stop)
        (self.root / 'url').write_text(self.fixture.start_http())

    def register(self, **changes):
        return live_connection.register(self.root, changes.get('target', self.fixture.target),
                                        changes.get('thread_id', self.thread), self.root)

    def test_register_pins_pane_child_and_host_generations_and_is_idempotent(self):
        receipt = self.register()
        again = self.register()
        self.assertEqual(receipt, again)
        record = read_record(self.root, receipt['session_id'])
        self.assertEqual(record['process'], '100:200')
        self.assertEqual(record['live_native_process'], '500:600')
        self.assertEqual(record['live_host_process'], '900:1000')
        self.assertEqual(record['live_owner']['agent_process'], '500:600')
        self.assertEqual(os.stat(record_path(self.root, receipt['session_id'])).st_mode & 0o777, 0o600)
        bridge = live_connection.resolve(self.root, {'threadId': self.thread, 'sessionId': self.thread})
        saved = bridge.call('desk_create_document', {'path': 'connected.md', 'title': 'Connected', 'text': 'same owner'})
        self.assertEqual(self.fixture.app.visual.content(saved['workspace_id'], saved['version_id'])[0], b'same owner')

    def test_wrong_thread_target_and_unregistered_metadata_cannot_bind(self):
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.register(thread_id='22222222-2222-2222-2222-222222222222')
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.register(target={**self.fixture.target, 'identity': 'changed'})
        self.assertFalse((self.root / 'live-connections.json').exists())
        self.register()
        for meta in (None, {}, {'threadId': 'not-a-uuid'}, {'threadId': self.thread, 'sessionId': 'different'}):
            with self.assertRaises(ValueError):
                live_connection.resolve(self.root, meta)
        with self.assertRaisesRegex(ValueError, 'not connected'):
            live_connection.resolve(self.root, {'threadId': '22222222-2222-2222-2222-222222222222'})

    def test_replaced_native_child_under_same_shell_and_foreign_host_are_rejected(self):
        self.register()
        with patch('live_connection.parent_processes', return_value=iter(['901:1001'])):
            with self.assertRaisesRegex(ValueError, 'native MCP host'):
                live_connection.resolve(self.root, {'threadId': self.thread})
        self.native = '501:601'
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            live_connection.resolve(self.root, {'threadId': self.thread})
        self.assertFalse((self.root / 'must-not-write.md').exists())

    def test_stale_pid_generation_or_incomplete_registration_cannot_call(self):
        receipt = self.register()
        self.stamps['500'] = '500:601'
        with self.assertRaisesRegex(ValueError, 'process changed'):
            live_connection.resolve(self.root, {'threadId': self.thread})
        self.stamps['500'] = '500:600'
        path = record_path(self.root, receipt['session_id'])
        record = read_record(self.root, receipt['session_id'])
        del record['live_owner']
        live_connection.write_private(path, record)
        with self.assertRaisesRegex(ValueError, 'native MCP host'):
            live_connection.resolve(self.root, {'threadId': self.thread})

    def test_mcp_dispatch_uses_native_metadata_and_keeps_session_mode(self):
        message = {'id': 1, 'method': 'tools/call', 'params': {'name': 'desk_context', 'arguments': {}, '_meta': {'threadId': self.thread}}}
        output = io.StringIO()
        bridge = unittest.mock.Mock()
        bridge.call.return_value = {'references': []}
        with patch('sys.argv', ['desk_mcp.py', '--run-dir', str(self.root), '--live']), patch('sys.stdin', io.StringIO(json.dumps(message) + '\n')), patch('sys.stdout', output), patch('live_connection.resolve', return_value=bridge) as resolve:
            desk_mcp.main()
        resolve.assert_called_once_with(str(self.root), {'threadId': self.thread})
        bridge.call.assert_called_once_with('desk_context', {})
        self.assertNotIn('isError', json.loads(output.getvalue())['result'])
        bridge.reset_mock()
        with patch('sys.argv', ['desk_mcp.py', '--run-dir', str(self.root), '--session-id', 'existing']), patch('sys.stdin', io.StringIO(json.dumps(message) + '\n')), patch('sys.stdout', io.StringIO()), patch('desk_mcp.Bridge', return_value=bridge) as constructor, patch('live_connection.resolve') as resolve:
            desk_mcp.main()
        constructor.assert_called_once_with(str(self.root), 'existing')
        resolve.assert_not_called()
