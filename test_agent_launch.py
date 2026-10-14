from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import agent_launch


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = {'tab': '@1', 'pane': '%1', 'identity': 'old-identity'}
        self.old = {'id': '%1', 'identity': 'old-identity', 'provider': 'codex', 'agent': True,
                    'thread_id': 'old-thread', 'workspace': self.temp.name}
        self.new = {'id': '%2', 'identity': 'new-identity', 'provider': 'codex', 'agent': True,
                    'thread_id': 'new-thread', 'workspace': self.temp.name}
        self.pg = Mock(socket='/tmp/synthetic.sock', source_session='test', log=Path(self.temp.name) / 'events.jsonl')
        self.pg.input_lock = threading.Lock()
        self.pg.validate_pane.return_value = self.old
        self.before = {'tabs': [{'id': '@1', 'panes': [self.old]}], 'groups': [{'id': 'g1'}]}
        self.after = {**self.before, 'tabs': [*self.before['tabs'], {'id': '@2', 'panes': [self.new]}]}
        self.pg.observe.side_effect = [self.before, self.after]
        self.metadata = patch('agent_launch.thread_metadata', return_value={
            'old-thread': {'model': 'gpt-test', 'reasoning_effort': 'high'}}).start()
        self.addCleanup(patch.stopall)
        self.tmux = patch('agent_launch.tmux', return_value='@2\t%2').start()
        self.catalog = patch('agent_launch.provider_models', side_effect=lambda provider: [
            {'model': 'gpt-test', 'names': ['gpt-test', 'GPT-Test'], 'efforts': ['low', 'high']},
            {'model': 'gpt-6-astra', 'names': ['gpt-6-astra', 'GPT-6-Astra'], 'efforts': ['low', 'high']},
        ] if provider == 'codex' else [
            {'model': 'sonnet', 'names': ['sonnet'], 'efforts': ['low', 'high']}
        ]).start()

    def test_inherits_exact_thread_and_workspace(self):
        result = agent_launch.resolve_settings(self.pg, self.source)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['settings'], {'provider': 'codex', 'model': 'gpt-test', 'effort': 'high', 'cwd': self.temp.name})
        self.assertEqual(self.metadata.call_args.args[1], {'old-thread'})
        self.pg.validate_pane.assert_called_with('@1', '%1', 'old-identity')
        self.assertEqual(self.pg.validate_pane.call_count, 2)

    def test_provider_change_never_inherits_other_provider_settings(self):
        result = agent_launch.resolve_settings(self.pg, self.source, {'provider': 'claude'})
        self.assertEqual(result['missing'], ['model', 'effort'])
        self.assertEqual(result['settings']['cwd'], self.temp.name)

    def test_explicit_overrides(self):
        result = agent_launch.resolve_settings(self.pg, self.source, {
            'provider': 'claude', 'model': 'sonnet', 'effort': 'low', 'cwd': '/tmp'})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['settings']['model'], 'sonnet')
        argv = agent_launch.provider_command(result['settings'], '/tmp/test.sock', '/tmp/test')
        self.assertEqual(argv[argv.index('--effort') + 1], 'low')
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')

    def test_unknown_never_launches(self):
        self.metadata.return_value = {}
        result = agent_launch.launch_agent(self.pg, self.source)
        self.assertEqual(result['missing_settings'], ['model', 'effort'])
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['ok'])
        self.assertFalse(result['attempted'])
        self.tmux.assert_not_called()

    def test_claude_unknown_is_not_guessed(self):
        self.old['provider'] = 'claude'
        result = agent_launch.resolve_settings(self.pg, self.source)
        self.assertEqual(result['missing'], ['model', 'effort'])
        self.metadata.assert_not_called()

    def test_stale_source_never_launches(self):
        self.pg.validate_pane.side_effect = ValueError('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            agent_launch.launch_agent(self.pg, self.source)
        self.tmux.assert_not_called()

    def test_source_revalidated_after_metadata(self):
        self.pg.validate_pane.side_effect = [self.old, ValueError('changed')]
        with self.assertRaisesRegex(ValueError, 'changed'):
            agent_launch.launch_agent(self.pg, self.source)
        self.tmux.assert_not_called()

    def test_launch_only_succeeds_after_new_identity_observed(self):
        result = agent_launch.launch_agent(self.pg, self.source, group_id='g1')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['ok'])
        self.assertEqual(result['thread_id'], 'new-thread')
        self.assertFalse(result['retryable'])
        self.tmux.assert_called_once()
        self.assertIn('--model gpt-test', self.tmux.call_args.args[-1])
        self.assertIn('model_reasoning_effort=', self.tmux.call_args.args[-1])
        self.pg.validate_pane.assert_called_with('@2', '%2', 'new-identity')
        self.pg.groups.move.assert_called_once()
        self.pg.focus.assert_called_once_with('@2', '%2')

    def test_submission_without_identity_is_uncertain(self):
        self.new.update(agent=False, thread_id=None)
        result = agent_launch.launch_agent(self.pg, self.source, timeout=0)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['tab'], '@2')
        self.tmux.assert_called_once()
        self.pg.focus.assert_not_called()

    def test_existing_identity_is_not_success(self):
        self.new['thread_id'] = 'old-thread'
        result = agent_launch.launch_agent(self.pg, self.source)
        self.assertEqual(result['status'], 'uncertain')
        self.pg.focus.assert_not_called()

    def test_launch_transport_failure_is_uncertain_and_not_replayed(self):
        self.tmux.side_effect = RuntimeError('lost response')
        result = agent_launch.launch_agent(self.pg, self.source)
        self.assertEqual(result['status'], 'uncertain')
        self.assertTrue(result['attempted'])
        self.tmux.assert_called_once()

    def test_exited_launch_fails(self):
        self.pg.observe.side_effect = [self.before, self.before]
        result = agent_launch.launch_agent(self.pg, self.source)
        self.assertEqual(result['status'], 'failed')
        self.tmux.assert_called_once()

    def test_new_identity_changed_during_confirmation(self):
        self.pg.validate_pane.side_effect = [self.old, self.old, self.old, ValueError('changed')]
        result = agent_launch.launch_agent(self.pg, self.source)
        self.assertEqual(result['status'], 'uncertain')
        self.pg.focus.assert_not_called()

    def test_unknown_informal_model_blocks_instead_of_inheriting(self):
        result = agent_launch.launch_agent(self.pg, self.source, {'model': 'Astra', 'effort': 'high'})
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['missing_settings'], ['model'])
        self.assertIn('gpt-6-astra', result['error'])
        self.assertEqual(result['settings']['model'], 'Astra')
        self.tmux.assert_not_called()

    def test_exact_catalog_display_name_is_canonicalized(self):
        result = agent_launch.resolve_settings(self.pg, self.source, {'model': 'GPT-6-Astra', 'effort': 'HIGH'})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['settings']['model'], 'gpt-6-astra')
        self.assertEqual(result['settings']['effort'], 'high')

    def test_unsupported_effort_blocks_before_launch(self):
        result = agent_launch.launch_agent(self.pg, self.source, {'effort': 'ultra'})
        self.assertEqual(result['missing_settings'], ['effort'])
        self.tmux.assert_not_called()

    def test_other_provider_model_is_blocked(self):
        result = agent_launch.launch_agent(self.pg, self.source, {'provider': 'claude', 'model': 'gpt-test', 'effort': 'high'})
        self.assertEqual(result['missing_settings'], ['model'])
        self.tmux.assert_not_called()

    def test_catalog_failure_blocks_without_fallback(self):
        self.catalog.side_effect = RuntimeError('unavailable')
        result = agent_launch.launch_agent(self.pg, self.source)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('catalog is unavailable', result['error'])
        self.tmux.assert_not_called()

    def test_catalog_read_revalidates_source(self):
        self.catalog.side_effect = lambda provider: setattr(self.pg.validate_pane, 'side_effect', ValueError('changed')) or []
        with self.assertRaisesRegex(ValueError, 'changed'):
            agent_launch.resolve_settings(self.pg, self.source)
        self.tmux.assert_not_called()

    def test_bad_cwd_or_source_fails_before_launch(self):
        for overrides in ({'cwd': 'relative'}, {'cwd': '/does-not-exist'}, {'model': ''}):
            with self.assertRaises(ValueError):
                agent_launch.launch_agent(self.pg, self.source, overrides)
        with self.assertRaises(ValueError):
            agent_launch.resolve_settings(self.pg, {'tab': '@1'})
        self.tmux.assert_not_called()


class CatalogTests(unittest.TestCase):
    def test_codex_catalog_pagination_and_cleanup(self):
        client = Mock()
        row = {'id': 'gpt-one', 'model': 'gpt-one', 'displayName': 'GPT One',
               'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}
        client.call.side_effect = [{'data': [row], 'nextCursor': 'page2'}, {'data': [], 'nextCursor': None}]
        with patch('agent_launch.ModelConnection', return_value=client):
            models = agent_launch.provider_models('codex')
        self.assertEqual(models[0]['efforts'], ['high'])
        self.assertEqual(client.call.call_args.args[1]['cursor'], 'page2')
        client.close.assert_called_once()

    def test_codex_catalog_repeating_cursor_fails_and_closes(self):
        client = Mock()
        client.call.return_value = {'data': [], 'nextCursor': 'same'}
        with patch('agent_launch.ModelConnection', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'did not advance'):
                agent_launch.provider_models('codex')
        client.close.assert_called_once()

    def test_claude_aliases_and_efforts_come_from_installed_help(self):
        help_text = "  --effort <level> Effort level (low, high)\n  --example Example\n  --model <model> An alias (e.g.\n       'fable', 'opus', or 'sonnet') or a full name.\n  -n, --name Name\n"
        with patch('agent_launch.subprocess.run', return_value=Mock(stdout=help_text)):
            models = agent_launch.provider_models('claude')
        self.assertEqual([model['model'] for model in models], ['fable', 'opus', 'sonnet'])
        self.assertEqual(models[0]['efforts'], ['low', 'high'])


if __name__ == '__main__':
    unittest.main()
