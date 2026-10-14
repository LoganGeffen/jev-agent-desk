import unittest
from unittest.mock import Mock, patch

import jev
from test_playground import answer


class RoutingTests(unittest.TestCase):
    def payload(self, request):
        return jev.question(request, [
            {'id': '@a', 'name': 'Alpha', 'agent': True,
             'panes': [{'id': '%a', 'number': 1, 'active': True, 'agent': True}]},
            {'id': '@b', 'name': 'Beta', 'agent': True,
             'panes': [{'id': '%b', 'number': 1, 'active': True, 'agent': True}]},
        ], '@a')

    def test_long_direct_message_keeps_every_word_without_boundary_questions(self):
        text = '  Okay, please fix this. ' + 'Beta had a problem; explain and correct it. ' * 400 + '\n'
        payload = self.payload(text)
        evaluate = Mock(return_value={'answers': {'intent': {'choice': 'direct_message'}}})
        response = jev.interpret(payload, evaluate, [])
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(set(evaluate.call_args.args[0]['questions']), {'intent'})
        self.assertEqual(response['answers']['target']['choice'], '@a')
        self.assertEqual(response['answers']['pane:@a']['choice'], '%a')
        self.assertEqual(jev.message_text(text, payload, response), text)
        self.assertIsNone(jev.refine_boundaries(payload, response, evaluate))
        self.assertEqual(evaluate.call_count, 1)

    def test_routed_message_stage_cannot_switch_or_close_tabs(self):
        request = 'Tell Beta: Close Alpha after the tests pass.'
        payload = self.payload(request)
        send = answer('send_message', '@b', '%b')
        send['answers'].update(message_start={'choice': str(request.index('Close'))},
                               message_end={'choice': str(len(request))}, message_form={'choice': 'verbatim'})
        evaluate = Mock(side_effect=[{'answers': {'intent': {'choice': 'routed_message'}}}, send])
        response = jev.interpret(payload, evaluate, [])
        questions = evaluate.call_args.args[0]['questions']
        self.assertNotIn('action', questions)
        self.assertNotIn('close_scope', questions)
        self.assertEqual(jev.message_text(request, payload, response), 'Close Alpha after the tests pass.')
        self.assertEqual(response['answers']['target']['choice'], '@b')

    def test_controller_stage_excludes_message_extraction_and_delivery(self):
        payload = self.payload('Switch to Beta')
        evaluate = Mock(side_effect=[{'answers': {'intent': {'choice': 'controller'}}}, answer('select_tab', '@b')])
        response = jev.interpret(payload, evaluate, [])
        questions = evaluate.call_args.args[0]['questions']
        self.assertNotIn('message_start', questions)
        self.assertNotIn('send_message', questions['action']['criteria'])
        self.assertEqual(response['answers']['action']['choice'], 'select_tab')

    def test_cancel_ends_before_target_or_boundary_decisions(self):
        evaluate = Mock(return_value={'answers': {'intent': {'choice': 'no_action'}}})
        response = jev.interpret(self.payload('Never mind'), evaluate, [])
        self.assertEqual(response['answers']['action']['choice'], 'no_action')
        self.assertEqual(evaluate.call_count, 1)

    def test_inspection_cannot_send_or_interrupt_even_with_a_bad_model_answer(self):
        for action in ('read_reply', 'ask_session', 'send_message', 'interrupt_turn'):
            result = answer(action, '@a')
            result['answers']['subject_scope'] = {'choice': 'selected'}
            evaluate = Mock(side_effect=[{'answers': {'intent': {'choice': 'inspect'}}}, result])
            if action in ('read_reply', 'ask_session'):
                response = jev.interpret(self.payload('Explain Beta’s reply'), evaluate, [])
                self.assertEqual(response['answers']['action']['choice'], action)
            else:
                with self.assertRaises(ValueError):
                    jev.interpret(self.payload('Explain Beta’s reply'), evaluate, [])
            questions = evaluate.call_args.args[0]['questions']
            self.assertEqual(set(questions['action']['criteria']), {'read_reply', 'ask_session'})
            self.assertNotIn('message_start', questions)

    def test_outer_read_then_send_is_not_collapsed_into_a_message(self):
        result = answer('send_message', '@b')
        result['answers']['delivery'] = {'choice': 'no_action'}
        evaluate = Mock(side_effect=[{'answers': {'intent': {'choice': 'routed_message'}}}, result])
        response = jev.interpret(self.payload('Read it then send Beta a request to fix it'), evaluate, [])
        self.assertEqual(response['answers']['action']['choice'], 'no_action')


class CreateTabRoutingTests(unittest.TestCase):
    def test_uncertain_clear_does_not_claim_nothing_was_sent(self):
        from test_playground import PlaygroundTests
        fixture = PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before = fixture.app.observe()
        tab = next(tab for tab in before['tabs'] if tab['id'] == before['selected'])
        pane = next(pane for pane in tab['panes'] if pane['active'])
        target = {'tab': tab['id'], 'pane': pane['id'], 'identity': pane['identity']}
        result = {'ok': False, 'status': 'uncertain', 'error': 'Clear input is unconfirmed. No automatic retry.'}
        with patch.object(fixture.app.workspace_actions, 'run', return_value=result):
            event = fixture.app.submit('/clear', request_id='clear-once', capture_target=target)
        self.assertEqual(event['delivery'], 'uncertain')
        self.assertEqual(event['control_result'], result)
        self.assertEqual(event['outcome'], 'error')

    def test_workspace_create_action_uses_captured_source_and_shared_launcher(self):
        from test_playground import PlaygroundTests
        fixture = PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before = fixture.app.observe()
        tab = next(tab for tab in before['tabs'] if tab['id'] == before['selected'])
        pane = next(pane for pane in tab['panes'] if pane['active'])
        fixture.response = answer('create_tab', tab['id'], pane['id'])
        result = {'ok': True, 'status': 'completed', 'tab': '@new', 'pane': '%new', 'identity': 'new'}
        with patch('server.launch_overrides', return_value={'provider': 'codex'}) as settings, \
             patch.object(fixture.app.workspace_actions, 'run', return_value=result) as launch:
            event = fixture.app.submit('Create a new Codex tab')
        settings.assert_called_once()
        self.assertEqual(launch.call_args.args[1], 'launch')
        self.assertEqual(launch.call_args.args[2], {'tab': tab['id'], 'pane': pane['id'], 'identity': pane['identity']})
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(event['view_target'], {'tab': '@new', 'pane': '%new', 'identity': 'new'})
