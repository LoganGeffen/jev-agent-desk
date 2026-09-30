import copy
import unittest
from unittest.mock import patch

import jev
from reply_watch import matching_reply
import test_playground
from test_playground import answer


class AdaptiveTests(unittest.TestCase):
    def test_unresolved_native_agent_keeps_intent_but_blocks_delivery(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        before = app.observe()
        tab = before['tabs'][0]
        tab.update(agent=False, codex=False)
        pane = tab['panes'][0]
        pane.update(command='codex', agent=False, codex=False, thread_id=None)
        payloads = []
        def evaluate(payload):
            payloads.append(payload)
            response = answer('send_message', tab['id'], pane['id'])
            response['answers'].update(message_start={'choice': '0'},
                                       message_end={'choice': str(len(payload['state']['request']))},
                                       message_form={'choice': 'verbatim'})
            return response
        app.evaluate = evaluate
        with patch.object(app, 'observe', return_value=before), patch('server.send_message') as send:
            result = app.submit('Please ask this agent to check my tasks.')
        routing = payloads[0]['state']['tabs']
        self.assertTrue(routing[0]['panes'][0]['agent'])
        self.assertFalse(routing[0]['panes'][0]['identity_ready'])
        self.assertFalse(routing[1]['panes'][0]['agent'])
        send.assert_not_called()
        self.assertEqual(result['delivery'], 'not_attempted')
        self.assertEqual(result['outcome'], 'error')
        self.assertIn('not available yet', result['error'])

    def test_unresolved_agent_no_action_explains_non_delivery(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        before = app.observe()
        tab = before['tabs'][0]
        tab.update(agent=False, codex=False)
        tab['panes'][0].update(command='codex', agent=False, codex=False, thread_id=None)
        app.evaluate = lambda payload: answer('no_action', tab['id'])
        with patch.object(app, 'observe', return_value=before), patch('server.send_message') as send:
            result = app.submit('Please ask this agent to check my tasks.')
        send.assert_not_called()
        self.assertEqual(result['delivery'], 'not_attempted')
        self.assertIn('conversation identity is unavailable', result['output'])

    def test_direct_address_is_distinct_from_a_topic_mention(self):
        tabs = [{'id': '@0', 'name': 'JEV controller', 'agent': True},
                {'id': '@1', 'name': 'dog data', 'agent': True}]
        for request in ('I was working in dog data. Can you investigate?',
                        'Please inspect dog data, including its logs.'):
            self.assertNotIn('direct_address', jev.question(request, tabs, '@0')['state'])
        payload = jev.question('Dog data, please investigate the microphone.', tabs, '@0')
        self.assertEqual(payload['state']['direct_address']['recipient_tab'], '@1')
        self.assertEqual(payload['state']['selected_tab'], '@0')
        corrected = jev.question('Dog data, actually switch to JEV controller.', tabs, '@0')
        self.assertIn('select_tab', corrected['questions']['action']['criteria'])

    def test_long_boundaries_are_bounded_and_copy_exact_source(self):
        request = 'Tell Luna: "' + ' '.join(f'word{i}' for i in range(600)) + '"'
        payload = jev.question(request, [{'id': '@0', 'name': 'Luna', 'agent': True}], '@0')
        self.assertTrue(all(len(q['criteria']) <= 255 for q in payload['questions'].values()))
        start, end = request.index('word0'), len(request) - 1
        response = answer('send_message', '@0')
        for name, offset in [('message_start', start), ('message_end', end)]:
            response['answers'][name] = {'choice': next(k for k, v in payload['questions'][name]['criteria'].items()
                                                      if v['first_offset'] <= offset <= v['last_offset'])}
        def refine(question):
            self.assertEqual(question['state']['request'], request)
            self.assertEqual(question['state']['routing_decision']['action'], 'send_message')
            self.assertTrue(all(len(q['criteria']) <= 255 for q in question['questions'].values()))
            return {'answers': {'message_start': {'choice': str(start)}, 'message_end': {'choice': str(end)}}}
        evidence = jev.refine_boundaries(payload, response, refine)
        self.assertIsNotNone(evidence)
        self.assertEqual(jev.message_text(request, payload, response), request[start:end])

    def test_long_no_action_never_refines_or_sends(self):
        payload = jev.question('word ' * 300, [{'id': '@0'}], '@0')
        with patch('jev.evaluate') as evaluate:
            self.assertIsNone(jev.refine_boundaries(payload, answer('no_action'), evaluate))
            evaluate.assert_not_called()

    def test_delivery_stage_survives_post_send_observation_failure(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        before = app.observe()
        tab = before['tabs'][0]
        pane = tab['panes'][0]
        pane.update(agent=True, codex=True, thread_id='fixture-thread', process='fixture-process', workspace='/tmp')
        tab.update(agent=True, codex=True)
        response = answer('send_message', tab['id'], pane['id'])
        response['answers'].update(message_start={'choice': '0'}, message_end={'choice': '5'}, message_form={'choice': 'verbatim'})
        app.evaluate = lambda payload: copy.deepcopy(response)
        with patch.object(app, 'observe', side_effect=[before, RuntimeError('snapshot failed')]), \
                patch.object(app, 'validate_pane'), patch('server.send_message') as send:
            result = app.submit('Hello', request_id='one')
        send.assert_called_once()
        self.assertEqual(result['delivery'], 'submitted')
        self.assertEqual(result['outcome'], 'ok')
        self.assertEqual(result['observation_error'], 'snapshot failed')
        self.assertEqual(app.recent_requests['one']['delivery'], 'submitted')
        with patch.object(app, 'observe', return_value=before), patch.object(app, 'validate_pane'), \
                patch('server.send_message', side_effect=RuntimeError('failed after paste')):
            result = app.submit('Hello', request_id='two')
        self.assertEqual(result['delivery'], 'uncertain')
        self.assertEqual(result['outcome'], 'error')
        app.evaluate = lambda payload: (_ for _ in ()).throw(RuntimeError('inference failed'))
        with patch('server.send_message') as send:
            result = app.submit('Hello', request_id='three')
        self.assertEqual(result['delivery'], 'not_attempted')
        send.assert_not_called()
        self.assertEqual(set(app.recent_requests), {'one', 'two', 'three'})

    def test_typed_and_voice_share_delivery_watch_and_preserve_view(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        app.independent_selection = True
        before = app.observe()
        tab = before['tabs'][1]
        pane = tab['panes'][0]
        tab.update(agent=True, codex=True)
        pane.update(agent=True, codex=True, thread_id='fixture-thread', process='fixture-process', workspace='/tmp')
        response = answer('send_message', tab['id'], pane['id'])
        response['answers'].update(message_start={'choice': '0'}, message_end={'choice': '5'}, message_form={'choice': 'verbatim'})
        app.evaluate = lambda payload: copy.deepcopy(response)
        for source in ('typed', 'voice'):
            with patch.object(app, 'observe', return_value=before), patch.object(app, 'validate_pane'), \
                    patch('server.reply_history', return_value={'turns': []}), \
                    patch.object(app.reply_watch, 'start') as watch, patch.object(app, 'focus') as focus, \
                    patch('server.send_message') as send:
                event = app.submit('Hello', source=source, request_id=source)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], 'Hello')
            watch.assert_called_once()
            self.assertEqual(watch.call_args.args[-1], source)
            focus.assert_not_called()
            self.assertEqual(event['delivery'], 'submitted')
            self.assertEqual(app.recent_requests[source]['delivered_text'], 'Hello')

    def test_capture_identity_change_rejects_before_interpretation(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state = fixture.app.observe()
        tab = state['tabs'][0]
        with patch.object(fixture.app, 'evaluate') as evaluate:
            event = fixture.app.submit('Hello', capture_target={'tab': tab['id'], 'pane': tab['panes'][0]['id'], 'identity': 'stale'})
        self.assertEqual(event['delivery'], 'not_attempted')
        evaluate.assert_not_called()


class ReplyEvidenceTests(unittest.TestCase):
    def turn(self, id, status='completed', text='fixture', reply='Done', phase='final_answer'):
        return {'id': id, 'status': status, 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]},
            {'type': 'agentMessage', 'phase': phase, 'text': reply}]}

    def test_matches_new_native_final_not_old_reply_or_idle(self):
        old = self.turn('old')
        self.assertIsNone(matching_reply({'turns': [old]}, {'old'}, 'fixture'))
        for turn in [self.turn('new', status='interrupted'), self.turn('new', status='inProgress'),
                     self.turn('new', phase='commentary'), self.turn('new', text='other input')]:
            self.assertIsNone(matching_reply({'turns': [old, turn]}, {'old'}, 'fixture'))
        result = matching_reply({'turns': [old, self.turn('new')]}, {'old'}, 'fixture')
        self.assertEqual(result, {'turn_id': 'new', 'text': 'Done'})
        self.assertIsNone(matching_reply({'turns': [self.turn('a'), self.turn('b')]}, set(), 'fixture'))
