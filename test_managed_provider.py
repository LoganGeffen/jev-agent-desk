import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk_mcp import Bridge
from managed_provider import ManagedProviders, record_path
from managed_session import provider_command
from test_visual_workspace import VisualTests
from visual import capture
from visual_workspace import VisualError, VisualWorkspace


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = VisualTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.visual = self.fixture.visual

    def test_completed_event_is_idempotent_and_conflicting_reuse_fails(self):
        body = self.fixture.body(receipt_key='completed-42')
        first = self.visual.register(body)
        self.assertEqual(first, VisualWorkspace(self.fixture.app).register(body))
        self.assertEqual(len(self.visual.list('@1')['creations'][0]['versions']), 1)
        with self.assertRaisesRegex(VisualError, 'different bytes'):
            self.visual.register(self.fixture.body(b'Changed', receipt_key='completed-42'))
        self.fixture.panes['%1']['identity'] = 'replaced'
        with self.assertRaises(VisualError):
            self.visual.register(body)

    def test_markdown_assets_archive_and_saved_reference(self):
        root = self.fixture.root
        (root / 'report.md').write_text('# Report\n\n![plot](plot.png)')
        (root / 'plot.png').write_bytes(b'PNG fixture')
        entrypoint, files = capture(root / 'report.md')
        receipt = self.visual.register(self.fixture.body(kind='markdown', entrypoint=entrypoint, files=files))
        (root / 'report.md').unlink()
        (root / 'plot.png').unlink()
        archive = self.visual.list_workspace(receipt['workspace_id'])
        self.assertTrue(archive['archived'])
        self.assertIsNone(archive['live'])
        version = archive['creations'][0]['versions'][0]
        url = version['assets']['plot.png']
        self.assertEqual(self.visual.preview(url.split('/')[2], 'plot.png')[0], b'PNG fixture')
        reference = self.visual.reference({**receipt, 'source': {'provider': 'forged'}})
        self.assertEqual(reference['source']['provider'], 'codex')
        self.fixture.owner_generation = 'recycled'
        self.assertEqual(self.visual.list('@1')['creations'], [])
        self.assertEqual(self.visual.list_workspace(receipt['workspace_id'])['creations'][0]['id'], receipt['creation_id'])

    def test_reference_binds_actual_recipient_and_keeps_literal_separate(self):
        receipt = self.visual.register(self.fixture.body())
        app = self.fixture.app
        app.visual = self.visual
        app.validate_pane = lambda tab, pane, identity=None: self.fixture.validate_pane(tab, pane, identity or self.fixture.panes[pane]["identity"])
        providers = ManagedProviders(app)
        providers.stage_reference('request-1', receipt)
        agent = {'window_id': '@2', 'pane_id': '%2', **self.fixture.panes['%2']}
        providers.bind_reference('request-1', agent)
        context = providers.context(agent)
        self.assertEqual(context['references'][0]['source']['provider'], 'codex')
        self.assertEqual(providers.context(self.fixture.panes['%1'])['references'], [])
        with self.assertRaisesRegex(VisualError, 'recipient changed'):
            providers.bind_reference('request-1', {'window_id': '@1', 'pane_id': '%1', **self.fixture.panes['%1']})

    def test_provider_config_is_session_local_and_native_resume(self):
        for provider in ('claude', 'codex'):
            argv = provider_command(provider, '/tmp/desk-run', 'a' * 32, '/tmp/desk.sock', resume='existing-thread')
            self.assertIn('existing-thread', argv)
            self.assertIn('--resume' if provider == 'claude' else 'resume', argv)
            self.assertTrue(any('desk_mcp.py' in arg for arg in argv))
            self.assertNotIn('--tools', argv)
        self.assertIn('--no-daemon', provider_command('codex', '/tmp/run', 'a' * 32, '/tmp/socket'))

    def test_new_tab_tools_preserve_provider_settings_and_effort(self):
        for provider in ('claude', 'codex'):
            argv = provider_command(provider, '/tmp/run', 'a' * 32, '/tmp/socket',
                                    model='test-model', effort='high', inherit_settings=True)
            self.assertTrue(any('desk_mcp.py' in arg for arg in argv))
            self.assertEqual(argv[argv.index('--model') + 1], 'test-model')
            for option in ('--sandbox', '--ask-for-approval', '--permission-mode', '--setting-sources', '--strict-mcp-config'):
                self.assertNotIn(option, argv)
            if provider == 'codex':
                self.assertIn('model_reasoning_effort="high"', argv)
            else:
                self.assertEqual(argv[argv.index('--effort') + 1], 'high')

    def test_bridge_cannot_capture_outside_workspace_or_symlink(self):
        root = self.fixture.root
        identifier = 'a' * 32
        path = record_path(root, identifier)
        path.parent.mkdir()
        path.write_text(json.dumps({'workspace': str(root), 'token': 'synthetic'}))
        bridge = Bridge(root, identifier)
        with self.assertRaises(ValueError):
            bridge.path('../elsewhere.txt')
        (root / 'alias').symlink_to('/tmp')
        with self.assertRaises(ValueError):
            bridge.path('alias/elsewhere.txt')
