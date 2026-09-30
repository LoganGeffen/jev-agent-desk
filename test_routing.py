import unittest
from unittest.mock import Mock

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


class CreateTabRoutingTests(unittest.TestCase):
    def test_workspace_create_action_opens_one_selected_terminal(self):
        from test_playground import PlaygroundTests
        fixture = PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.response = answer('create_tab')
        event = fixture.app.submit('Create a new tab')
        self.assertEqual(event['outcome'], 'ok', event['error'])
        self.assertEqual(len(event['after']['tabs']), len(event['before']['tabs']) + 1)
        selected = next(tab for tab in event['after']['tabs'] if tab['id'] == event['after']['selected'])
        self.assertEqual(selected['name'], 'Terminal')
