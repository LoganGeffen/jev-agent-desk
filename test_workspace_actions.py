import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jev
from jarvis_controller import JarvisController
from jarvis_tools import JarvisTools, TOOLS
from server import Playground, make_server
from workspace_actions import WorkspaceActions, command_text, plan


A = {'tab': '@a', 'pane': '%a', 'identity': 'a:original'}
B = {'tab': '@b', 'pane': '%b', 'identity': 'b:original'}


def fixture_app():
    tabs = [{'id': target['tab'], 'name': name, 'group_id': 'work', 'agent': True,
             'panes': [{'id': target['pane'], 'number': 1, 'active': True, 'agent': True,
                        'identity': target['identity'], 'handoff': {}}]}
            for target, name in ((A, 'Alpha'), (B, 'Beta'))]
    return SimpleNamespace(observe=Mock(return_value={'tabs': tabs}), validate_pane=Mock(),
                           evaluate=Mock(), input_lock=threading.RLock())


def answers(**choices):
    return {'answers': {key: {'choice': value} for key, value in choices.items()}}


def controller_evaluator(target):
    def evaluate(question):
        if 'intent' in question['questions']:
            return answers(intent='controller')
        if 'command' in question['questions']:
            return answers(command='/clear')
        return answers(action='agent_command', target=target['tab'], **{'pane:' + target['tab']: target['pane']})
    return evaluate


class WorkspaceActionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'actions.sqlite3'
        self.app = fixture_app()

    def store(self):
        store = WorkspaceActions(self.app, self.path)
        self.addCleanup(store.db.close)
        return store

    def test_literal_slash_uses_captured_subject_without_model_and_quoted_slash_is_message(self):
        operation, target, options = plan(self.app, '/clear', A)
        self.assertEqual((operation, target, options), ('command', A, {'command': '/clear'}))
        self.app.evaluate.assert_not_called()
        request = '"/clear"'
        payload = jev.question(request, self.app.observe()['tabs'], '@a')
        self.app.evaluate.return_value = answers(intent='direct_message')
        interpreted = jev.interpret(payload, self.app.evaluate, [])
        self.assertEqual(interpreted['answers']['action']['choice'], 'send_message')
        self.assertEqual(jev.message_text(request, payload, interpreted), request)
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            plan(self.app, request, A)

    def test_natural_command_named_target_and_unnamed_subject(self):
        self.app.evaluate.side_effect = controller_evaluator(B)
        self.assertEqual(plan(self.app, 'Clear Beta', A)[1], B)
        self.app.evaluate.side_effect = controller_evaluator(A)
        self.assertEqual(plan(self.app, 'Clear this conversation', A)[1], A)
        self.app.validate_pane.assert_any_call('@b', '%b', 'b:original')

    def test_explicit_named_target_does_not_depend_on_unrelated_stale_subject(self):
        def validate(window, pane, identity):
            if window == '@a':
                raise ValueError('Alpha identity changed')
        self.app.validate_pane.side_effect = validate
        self.app.evaluate.side_effect = controller_evaluator(B)
        self.assertEqual(plan(self.app, 'Clear Beta', A)[1], B)

    def test_stale_subject_or_target_blocks_execution(self):
        store = self.store()
        store.command = Mock()
        self.app.validate_pane.side_effect = ValueError('Identity changed')
        result = store.run('stale', 'command', A, {'command': '/clear'})
        self.assertEqual(result['status'], 'blocked')
        store.command.assert_not_called()
        with self.assertRaises(ValueError):
            plan(self.app, '/clear', A)

    def test_unnamed_command_preserves_captured_identity_after_snapshot_replacement(self):
        self.app.observe.return_value['tabs'][0]['panes'][0]['identity'] = 'a:replacement'
        self.app.validate_pane.side_effect = ValueError('Captured identity changed')
        with self.assertRaisesRegex(ValueError, 'Captured identity changed'):
            plan(self.app, '/clear', A)
        self.app.validate_pane.assert_called_once_with('@a', '%a', 'a:original')

    def test_replay_survives_reopen_and_rejects_changed_input(self):
        store = self.store()
        store.command = Mock(return_value={'ok': True, 'status': 'submitted', 'output': 'status'})
        expected = store.run('same', 'command', A, {'command': '/status'})
        reopened = self.store()
        reopened.command = Mock()
        self.assertEqual(reopened.run('same', 'command', A, {'command': '/status'}), expected)
        reopened.command.assert_not_called()
        store.command.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'different input'):
            reopened.run('same', 'command', B, {'command': '/status'})

    def test_restart_during_attempt_retains_uncertainty_and_does_not_replay(self):
        store = self.store()
        payload = json.dumps(['command', A, {'command': '/clear'}], sort_keys=True)
        store.db.execute('INSERT INTO operations VALUES (?, ?, NULL)', ('crash', payload))
        store.db.commit()
        reopened = self.store()
        reopened.command = Mock()
        result = reopened.run('crash', 'command', A, {'command': '/clear'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'uncertain')
        reopened.command.assert_not_called()
        self.app.validate_pane.assert_not_called()

    def test_exception_after_attempt_is_durable_uncertain(self):
        store = self.store()
        store.command = Mock(side_effect=RuntimeError('connection lost'))
        first = store.run('lost', 'command', A, {'command': '/clear'})
        second = store.run('lost', 'command', A, {'command': '/clear'})
        self.assertEqual(first['status'], 'uncertain')
        self.assertEqual(first, second)
        store.command.assert_called_once()

    def test_unsupported_command_and_multiline_do_not_reach_native_input(self):
        store = self.store()
        self.app.validate_pane.return_value = {'agent': True, 'thread_id': 'thread', 'provider': 'codex'}
        with patch('workspace_actions.adapter') as native, patch('workspace_actions.tmux') as tmux:
            result = store.run('unsupported', 'command', A, {'command': '/unsupported'})
            self.assertEqual(result['status'], 'blocked')
            native.assert_not_called()
            tmux.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'one slash'):
            command_text('/status\n/clear', self.app.evaluate)

    def test_jarvis_authorizes_from_current_text_and_executes_saved_plan_once(self):
        tools = JarvisTools(self.app)
        tools.validate = Mock(side_effect=lambda target: target)
        self.app.workspace_actions = self.store()
        self.app.workspace_actions.command = Mock(return_value={'ok': True, 'status': 'submitted', 'output': 'Status submitted'})
        turn = {'turn_id': 'turn', 'request_id': 'request', 'user_text': '/status', 'subject': A}
        self.assertTrue(tools.authorize('workspace_action', {}, turn))
        first = tools.execute('workspace_action', {}, turn)
        second = tools.execute('workspace_action', {}, turn)
        self.assertEqual(first, second)
        self.assertEqual(first['subject'], A)
        self.app.workspace_actions.command.assert_called_once_with(A, '/status')
        denied = {**turn, 'turn_id': 'other', 'user_text': 'Explain why /clear exists'}
        self.app.evaluate.return_value = answers(intent='direct_message')
        self.assertFalse(tools.authorize('workspace_action', {}, denied))

    def test_controller_effect_gate_denies_workspace_action_without_execution(self):
        results = []
        def provider(context, specs, execute, **kwargs):
            results.append(execute('workspace_action', {}))
            return {'text': 'No change.'}
        execute = Mock()
        controller = JarvisController(provider, execute, TOOLS, authorize=lambda *args: False,
                                      effect_tools={'workspace_action'})
        controller.submit('Tell Alpha: explain /clear', request_id='deny')
        self.assertEqual(results[0]['status'], 'not_attempted')
        execute.assert_not_called()
        self.assertFalse(controller.snapshot()['actions'])

    def test_details_revalidates_identity_after_provider_read(self):
        app = SimpleNamespace(validate_pane=Mock(side_effect=[
            {'provider': 'codex', 'thread_id': 'thread'}, ValueError('Identity changed during read')]))
        with patch('server.conversation_details', return_value={'text': 'private output'}) as read:
            with self.assertRaisesRegex(ValueError, 'changed during read'):
                Playground.conversation_details(app, **A, turn_id='turn', item_index=0, item_key='a' * 64)
            read.assert_called_once()
        self.assertEqual(app.validate_pane.call_count, 2)

    def test_details_stale_identity_fails_before_provider_read(self):
        app = SimpleNamespace(validate_pane=Mock(side_effect=ValueError('Stale identity')))
        with patch('server.conversation_details') as read:
            with self.assertRaisesRegex(ValueError, 'Stale identity'):
                Playground.conversation_details(app, **A, turn_id='turn', item_index=0, item_key='a' * 64)
            read.assert_not_called()


class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = fixture_app()
        self.app.push = Mock()
        self.path = Path(self.directory.name) / 'actions.sqlite3'
        self.app.workspace_actions = WorkspaceActions(self.app, self.path)
        self.addCleanup(self.app.workspace_actions.db.close)
        self.app.workspace_actions.command = Mock(return_value={'ok': True, 'status': 'submitted'})
        for name in ('VisualWorkspace', 'create_browser', 'ManagedProviders', 'ManagedApps'):
            mocked = patch('server.' + name)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name in ('browser_post', 'provider_post', 'apps_post', 'visual_post'):
            mocked = patch('server.' + name, return_value=False)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.body = {'request_id': 'http', 'operation': 'command', 'target': A, 'arguments': {'command': '/status'}}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def post(self, body, origin=None):
        request = Request(self.url + '/api/actions', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', 'Origin': origin or self.url})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_http_replay_uses_durable_receipt_after_store_reopen(self):
        status, first = self.post(self.body)
        self.assertEqual(status, 200)
        self.assertEqual(first['status'], 'submitted')
        first_command = self.app.workspace_actions.command
        self.app.workspace_actions = WorkspaceActions(self.app, self.path)
        self.addCleanup(self.app.workspace_actions.db.close)
        self.app.workspace_actions.command = Mock()
        self.assertEqual(self.post(self.body), (200, first))
        first_command.assert_called_once()
        self.app.workspace_actions.command.assert_not_called()

    def test_http_rejects_unknown_fields_missing_target_and_oversized_body(self):
        for body in ({**self.body, 'extra': True}, {**self.body, 'target': {'tab': '@a'}},
                     {**self.body, 'arguments': {'command': '/status', 'extra': True}},
                     {**self.body, 'request_id': 'x' * 9000},
                     {**self.body, 'request_id': 'x' * 257},
                     {**self.body, 'target': {**A, 'identity': None}},
                     {**self.body, 'arguments': {'command': ['/clear']}}):
            with self.subTest(body_keys=list(body)):
                self.assertEqual(self.post(body)[0], 400)
        self.app.workspace_actions.command.assert_not_called()
        self.assertEqual(self.app.workspace_actions.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)

    def test_http_missing_required_command_is_rejected_without_durable_attempt(self):
        status, result = self.post({**self.body, 'arguments': {}})
        self.assertEqual(status, 400, result)
        self.app.workspace_actions.command.assert_not_called()
        self.assertEqual(self.app.workspace_actions.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)

    def test_http_foreign_origin_cannot_execute(self):
        self.assertEqual(self.post(self.body, origin='https://foreign.test')[0], 403)
        self.app.workspace_actions.command.assert_not_called()
