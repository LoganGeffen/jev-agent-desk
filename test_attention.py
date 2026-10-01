import copy
import unittest
from unittest.mock import Mock, patch

from attention import AttentionQueue
from reply_watch import ReadbackPreparation
import test_playground
from test_readback_preparation import turn


def agent(name):
    return {'window_id': '@' + name, 'pane_id': '%' + name, 'provider': 'codex',
            'thread_id': name, 'process': 'process-' + name}


def tab(name, state='ready'):
    a = agent(name)
    return {'id': a['window_id'], 'name': name, 'state': state, 'panes': [
        {'id': a['pane_id'], 'provider': a['provider'], 'thread_id': a['thread_id'],
         'process': a['process'], 'identity': 'identity-' + name, 'agent': True, 'number': 1}]}


def judge(payload):
    return {'answers': {id: {'choice': 'input' if text == 'Which option should I use?' else 'reply'}
                        for id, text in payload['state']['replies'].items()}}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = AttentionQueue()
        self.tabs = [tab('a'), tab('b')]

    def test_input_first_then_oldest_unread_and_heard_is_not_handled(self):
        self.queue.observe(agent('a'), {'turns': [turn('a1', 'All tests passed.')]})
        self.queue.observe(agent('b'), {'turns': [turn('b1', 'Which option should I use?')]})
        self.queue.classify(self.queue.view(self.tabs, include_heard=True), judge)
        items = self.queue.view(self.tabs)
        self.assertEqual([item['tab'] for item in items], ['@b', '@a'])
        self.queue.heard(items[0]['id'])
        self.queue.heard(items[1]['id'])
        self.assertEqual([item['tab'] for item in self.queue.view(self.tabs)], ['@b'])
        self.queue.observe(agent('b'), {'turns': [turn('b1', 'Which option should I use?'),
                                                turn('b2', '', 'inProgress')]})
        self.assertEqual(self.queue.view(self.tabs), [])
        self.queue.observe(agent('b'), {'turns': [turn('b2', 'Applied the choice.')]})
        self.assertFalse(self.queue.view(self.tabs)[0]['needs_input'])

    def test_arrival_order_dedup_and_late_ack_does_not_clear_new_reply(self):
        for name in ('b', 'a'):
            self.queue.observe(agent(name), {'turns': [turn('1', 'A reply.')]})
        before = self.queue.view(self.tabs)
        self.assertEqual([item['tab'] for item in before], ['@b', '@a'])
        self.queue.observe(agent('b'), {'turns': [turn('1', 'A reply.')]})
        self.assertEqual(self.queue.view(self.tabs), before)
        self.queue.observe(agent('b'), {'turns': [turn('2', 'Another reply.')]})
        self.queue.heard(before[0]['id'])
        self.assertEqual([item['tab'] for item in self.queue.view(self.tabs)], ['@a', '@b'])

    def test_submitted_answer_clears_old_decision_even_before_history_catches_up(self):
        thread = {'turns': [turn('1', 'Which option should I use?')]}
        self.queue.observe(agent('a'), thread)
        self.queue.classify(self.queue.view(self.tabs), judge)
        self.queue.answered(agent('a'))
        self.queue.observe(agent('a'), thread)
        self.assertEqual(self.queue.view(self.tabs), [])
        self.queue.observe(agent('a'), {'turns': [turn('2', 'Done.')]})
        self.assertEqual(len(self.queue.view(self.tabs)), 1)

    def test_replaced_closed_unavailable_and_running_sessions_are_not_recommended(self):
        self.queue.observe(agent('a'), {'turns': [turn('1', 'Reply.')]})
        self.tabs[0]['panes'][0]['process'] = 'replacement'
        self.assertEqual(self.queue.view(self.tabs), [])
        self.tabs[0] = tab('a', 'working')
        self.assertEqual(self.queue.view(self.tabs), [])
        self.tabs[0] = tab('a')
        self.queue.unavailable(agent('a'))
        self.assertEqual(self.queue.view(self.tabs), [])
        self.queue.retain([])
        self.assertEqual(self.queue.entries, {})

    def test_explicit_input_state_without_reply_is_selectable_not_readable(self):
        self.tabs[0]['state'] = 'needs_you'
        item = self.queue.view(self.tabs)[0]
        self.assertTrue(item['needs_input'])
        self.assertEqual(item['text'], '')

    def test_terminal_origin_and_both_providers_use_existing_watcher(self):
        agents = [agent('a'), {**agent('b'), 'provider': 'claude'}]
        self.tabs[1]['panes'][0]['provider'] = 'claude'
        watch = ReadbackPreparation(Mock(), self.queue)
        with patch('reply_watch.history', return_value={'turns': [turn('1', 'Complete reply.')]}):
            watch.poll(agents)
        self.assertEqual(len(self.queue.view(self.tabs)), 2)
        with patch('reply_watch.history', side_effect=RuntimeError('identity changed')):
            watch.poll(agents)
        self.assertEqual(self.queue.view(self.tabs), [])


class AttentionFlowTests(unittest.TestCase):
    def setUp(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.app = fixture.app
        self.tabs = copy.deepcopy(fixture.tabs)
        for tab in self.tabs:
            tab.update(agent=True, codex=True, state='ready')
            for pane in tab['panes']:
                pane.update(agent=True, codex=True, command='codex', provider='codex',
                            thread_id=tab['id'], process='process-' + pane['id'])
                pane['identity'] = pane['process'] + ':codex:' + tab['id']
        self.panes = patch.object(self.app, 'panes', side_effect=lambda window:
                                 next(t['panes'] for t in self.tabs if t['id'] == window))
        self.panes.start()
        self.addCleanup(self.panes.stop)
        self.app.evaluate = self.evaluate
        self.recommendation = None
        self.action = 'recommend_next'
        self.publish(0, 'old', 'Old reply.')
        self.publish(1, 'new', 'Which option should I use?')

    def publish(self, index, id, text):
        t = self.tabs[index]
        p = t['panes'][0]
        self.app.attention.observe({'window_id': t['id'], 'pane_id': p['id'],
            'provider': p['provider'], 'thread_id': p['thread_id'], 'process': p['process']},
            {'turns': [turn(id, text)]})

    def evaluate(self, payload):
        if 'replies' in payload['state']:
            return judge(payload)
        if 'intent' in payload['questions']:
            return {'answers': {'intent': {'choice': 'attention'}}}
        return {'answers': {'action': {'choice': self.action}}}

    def submit(self, action, text):
        self.action = action
        return self.app.submit(text, request_id=text, attention_target=self.recommendation)

    def test_recommend_list_followup_read_and_completion_ack(self):
        initial = self.app.observe()['selected']
        event = self.submit('recommend_next', "What's next?")
        self.assertEqual(event['outcome'], 'ok', event.get('error'))
        self.assertEqual(self.app.observe()['selected'], initial)
        self.recommendation = event['recommendation']
        self.assertEqual(self.recommendation['tab'], self.tabs[1]['id'])
        self.assertIn('needs your input', event['output'])
        rundown = self.submit('list_ready', 'What else is ready?')
        self.assertIn(self.tabs[0]['name'], rundown['output'])
        read = self.submit('read_recommended', 'Read it')
        self.assertEqual(read['output'], 'Which option should I use?')
        self.assertEqual(read['delivery'], 'not_attempted')
        self.assertEqual(self.app.observe()['selected'], self.tabs[1]['id'])
        self.assertEqual(self.app.recent_requests['Read it']['heard_reply'], read['heard_reply'])
        self.app.heard_reply(read['heard_reply'])
        self.assertEqual(len(self.app.attention.view(self.app.observe()['tabs'])), 2)
        go = self.submit('select_recommended', 'Go there')
        self.assertEqual(go['outcome'], 'ok', go.get('error'))

    def test_new_arrival_does_not_change_the_referenced_chat_and_replacement_blocks(self):
        event = self.submit('recommend_next', "What's next?")
        self.recommendation = event['recommendation']
        self.publish(2, 'later', 'Another reply.')
        self.assertEqual(self.submit('read_recommended', 'Read it')['output'], 'Which option should I use?')
        self.publish(1, 'replacement-reply', 'Different reply.')
        stale = self.submit('read_recommended', 'Read it again')
        self.assertEqual(stale['outcome'], 'error')
        self.assertEqual(stale['delivery'], 'not_attempted')

    def test_missing_reference_and_invalid_attention_action_never_send(self):
        with patch('server.send_message') as send:
            self.assertEqual(self.submit('select_recommended', 'Go there')['outcome'], 'error')
            self.assertEqual(self.submit('send_message', 'Bad model action')['outcome'], 'error')
        send.assert_not_called()

    def test_heard_receipt_cannot_clear_another_conversations_reply(self):
        items = self.app.attention.view(self.app.observe()['tabs'])
        forged = {**self.app.attention.reference(items[0]), 'id': items[1]['id']}
        self.app.heard_reply(forged)
        self.assertEqual(len(self.app.attention.view(self.app.observe()['tabs'])), 2)

    def test_legacy_notice_readback_clears_only_the_exact_reply(self):
        item = self.app.attention.view(self.app.observe()['tabs'])[0]
        notice = {**item, 'id': 'legacy-notice'}
        self.app.reply_watch.notices.append(notice)
        self.app.heard_reply({'notice_id': 'legacy-notice'})
        self.assertEqual(len(self.app.attention.view(self.app.observe()['tabs'])), 1)
        self.publish(0, 'fresh', 'A newer reply.')
        self.app.heard_reply({'notice_id': 'legacy-notice'})
        self.assertEqual(len(self.app.attention.view(self.app.observe()['tabs'])), 2)
