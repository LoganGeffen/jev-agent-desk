import threading
from types import SimpleNamespace
import unittest

from jarvis_controller import JarvisController
from jarvis_turns import JarvisTurns
from spoken_reply import ReadbackCache


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.choice = 'complete'
        self.correction = False
        self.effects = []
        self.app = SimpleNamespace(evaluate=self.evaluate, readbacks=ReadbackCache(), heard_reply=self.effects.append)
        self.app.jarvis = JarvisController(lambda *a, **k: {'text': 'Verified answer'}, lambda *a: {})
        self.app.jarvis_tools = SimpleNamespace(validate=lambda target: dict(target))
        self.turns = JarvisTurns(self.app)
        self.app.jarvis.turn_guard = self.turns.guard
        self.addCleanup(self.app.readbacks.pool.shutdown)

    def evaluate(self, payload):
        return {'answers': {'decision': {'choice': self.choice}, 'correction': {'choice': 'yes' if self.correction else 'no'}}}

    def body(self, text='Tell Dog Data: exact words', revision=1, **fields):
        return {'session_id': self.app.jarvis.state.session_id, 'client_id': 'phone', 'transcript_id': 'speech',
                'transcript_revision': revision, 'text': text, 'subject': None, **fields}

    def commit(self, decision, text='Tell Dog Data: exact words', request='send'):
        body = self.body(text)
        body.update(source='voice', request_id=request, decision_id=decision.get('decision_id'))
        self.turns.commit(body)
        return body

    def test_pause_holds_and_complete_correction_has_token_without_rewriting(self):
        self.choice = 'wait'
        partial = self.turns.decide(self.body('Tell Dog Data because'))
        self.assertEqual(partial['decision'], 'wait')
        self.assertNotIn('decision_id', partial)
        self.correction = True
        unfinished = self.turns.decide(self.body('No, tell Controller', 2))
        self.assertTrue(unfinished['correction'])
        self.assertNotIn('decision_id', unfinished)
        self.choice = 'complete'
        complete = self.turns.decide(self.body('No, tell Controller: revised words', 3))
        self.assertTrue(complete['correction'])
        self.assertIn('decision_id', complete)
        self.assertEqual(self.app.jarvis.snapshot()['turns'], {})
        self.assertEqual(self.turns.clients['phone']['text'], 'No, tell Controller: revised words')

    def test_voice_subject_is_captured_not_replaced_by_other_client_remembered_subject(self):
        target = {'tab': '@1', 'pane': '%1', 'identity': 'original', 'label': 'Controller'}
        other = {'tab': '@2', 'pane': '%2', 'identity': 'other', 'label': 'Dog Data'}
        decision = self.turns.decide(self.body(subject=target))
        self.app.jarvis.state.subject = other
        body = self.body(subject=target, source='voice', request_id='captured', decision_id=decision['decision_id'])
        self.turns.commit(body)
        contexts = []
        self.app.jarvis.provider = lambda context, *args, **kwargs: contexts.append(context) or {'text': 'Done'}
        result = self.app.jarvis.submit(body['text'], body['request_id'], source='voice', subject=target)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(contexts[0]['subject'], target)
        with self.assertRaises(ValueError):
            self.turns.commit({**body, 'request_id': 'different', 'subject': other})

    def test_new_words_or_onset_revoke_token_and_explicit_send_bypasses_hold(self):
        first = self.turns.decide(self.body())
        self.turns.decide(self.body(revision=2, invalidate=True))
        with self.assertRaises(ValueError):
            self.commit(first)
        explicit = self.body('unfinished because')
        explicit.update(source='voice', request_id='manual', explicit_send=True)
        self.turns.commit(explicit)
        result = self.app.jarvis.submit(explicit['text'], explicit['request_id'], source='voice')
        self.assertEqual(result['status'], 'completed')
        with self.assertRaises(ValueError):
            self.turns.decide(self.body(revision=1))

    def test_delayed_decision_cannot_commit_resumed_words_and_failure_waits(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def slow(payload):
            entered.set()
            release.wait(2)
            return self.evaluate(payload)
        self.app.evaluate = slow
        worker = threading.Thread(target=lambda: results.append(self.turns.decide(self.body())))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.turns.decide(self.body('more words', 2, invalidate=True))
        release.set()
        worker.join(2)
        self.assertEqual(results[0]['decision'], 'wait')
        self.assertNotIn('decision_id', results[0])
        self.app.evaluate = lambda payload: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.turns.decide(self.body(revision=3))['decision'], 'wait')

    def test_invalidation_during_inference_fences_effect_without_erasing_prior_receipt(self):
        decision = self.turns.decide(self.body())
        body = self.commit(decision)
        entered, release = threading.Event(), threading.Event()
        delivered = []
        def provider(context, specs, execute, **kwargs):
            entered.set()
            release.wait(2)
            execute('interrupt_agent', {})
            return {'text': 'Done'}
        self.app.jarvis.provider = provider
        self.app.jarvis.specs['interrupt_agent'] = {'inputSchema': {'type': 'object'}}
        self.app.jarvis.authorize = lambda *args: True
        self.app.jarvis.execute_tool = lambda *args: delivered.append(args) or {'ok': True, 'status': 'completed'}
        receipts = []
        worker = threading.Thread(target=lambda: receipts.append(self.app.jarvis.submit(body['text'], 'send', source='voice')))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.turns.decide(self.body(revision=2, invalidate=True))
        release.set()
        worker.join(2)
        self.assertEqual(delivered, [])
        self.assertEqual(receipts[0]['status'], 'cancelled')
        self.assertEqual(self.app.jarvis.submit(body['text'], 'send', source='voice')['turn_id'], receipts[0]['turn_id'])


    def pending_effect(self):
        decision = self.turns.decide(self.body())
        body = self.commit(decision)
        entered, release = threading.Event(), threading.Event()
        delivered, receipts = [], []
        def provider(context, specs, execute, **kwargs):
            if context['current_turn']['request_id'] == 'send':
                entered.set()
                release.wait(3)
                execute('interrupt_agent', {})
            return {'text': 'Application response'}
        core = self.app.jarvis
        core.provider = provider
        core.specs['interrupt_agent'] = {'inputSchema': {'type': 'object'}}
        core.authorize = lambda *args: True
        core.execute_tool = lambda *args: delivered.append(args) or {'ok': True, 'status': 'completed'}
        worker = threading.Thread(target=lambda: receipts.append(core.submit(body['text'], 'send', source='voice')))
        worker.start()
        self.assertTrue(entered.wait(1))
        return release, worker, delivered, receipts

    def test_new_uncommitted_filler_draft_preserves_consumed_authorization(self):
        release, worker, delivered, receipts = self.pending_effect()
        try:
            self.choice = 'wait'
            filler = self.turns.decide(self.body('Hmm.', transcript_id='filler'))
            self.assertEqual(filler['decision'], 'wait')
            self.turns.guard('send')
            self.assertEqual(self.app.jarvis.snapshot()['turns'][self.app.jarvis.state.requests['send']]['status'], 'running')
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(receipts[0]['status'], 'completed')

    def assert_withdrawal(self, choice, correction, text):
        release, worker, delivered, receipts = self.pending_effect()
        try:
            self.choice, self.correction = choice, correction
            result = self.turns.decide(self.body(text, transcript_id='replacement'))
            self.assertEqual(result['correction'], correction)
            with self.assertRaises(InterruptedError):
                self.turns.guard('send')
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(delivered, [])
        self.assertEqual(receipts[0]['status'], 'cancelled')

    def test_recognized_cancel_revokes_pending_effect(self):
        self.assert_withdrawal('cancel', False, 'Cancel that request.')

    def test_recognized_incomplete_correction_revokes_pending_effect(self):
        self.assert_withdrawal('wait', True, 'Actually, tell Controller to')

    def test_recognized_complete_correction_revokes_pending_effect(self):
        self.assert_withdrawal('complete', True, 'Actually, tell Controller to pause the queue.')

    def test_new_complete_decision_alone_is_not_dispatch_but_new_turn_supersedes(self):
        release, worker, delivered, receipts = self.pending_effect()
        try:
            text = 'What is ready?'
            decision = self.turns.decide(self.body(text, transcript_id='question'))
            self.turns.guard('send')
            body = self.body(text, transcript_id='question', source='voice', request_id='new',
                             decision_id=decision['decision_id'])
            self.turns.commit(body)
            newer = self.app.jarvis.submit(text, 'new', source='voice')
            self.assertEqual(newer['status'], 'completed')
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(delivered, [])
        self.assertEqual(receipts[0]['status'], 'cancelled')

    def test_another_client_correction_does_not_revoke_original_client(self):
        self.commit(self.turns.decide(self.body()))
        self.choice, self.correction = 'wait', True
        self.turns.decide(self.body('Replace that with', client_id='desktop', transcript_id='other'))
        self.turns.guard('send')

    def test_same_transcript_revision_invalidates_but_identical_recheck_does_not(self):
        self.commit(self.turns.decide(self.body()))
        self.turns.decide(self.body())
        self.turns.guard('send')
        self.turns.decide(self.body(revision=2, invalidate=True))
        with self.assertRaises(InterruptedError):
            self.turns.guard('send')

    def test_same_consumed_source_revision_revokes_even_after_a_different_filler_draft(self):
        self.commit(self.turns.decide(self.body()))
        self.choice = 'wait'
        self.turns.decide(self.body('Hmm.', transcript_id='filler'))
        self.turns.guard('send')
        with self.assertRaises(ValueError):
            self.turns.decide(self.body(revision=2, invalidate=True))
        with self.assertRaises(InterruptedError):
            self.turns.guard('send')

    def test_semantic_question_preserves_previous_accepted_exact_user_source(self):
        self.commit(self.turns.decide(self.body()))
        payloads = []
        self.app.evaluate = lambda payload: payloads.append(payload) or self.evaluate(payload)
        self.turns.decide(self.body('Actually, tell Controller to', transcript_id='new'))
        prior = payloads[0]['state']['previous_accepted_user_request']
        self.assertEqual(prior['text'], 'Tell Dog Data: exact words')
        self.assertEqual(prior['transcript_id'], 'speech')
        self.assertEqual(prior['transcript_revision'], 1)

    def test_playback_owns_source_and_cancelled_or_old_epoch_cannot_acknowledge(self):
        receipt = self.app.jarvis.submit('Hello', 'hello')
        create = {'turn_id': receipt['turn_id'], 'client_id': 'phone', 'epoch': 1, 'style': 'original'}
        playback = self.turns.create_playback(create)
        self.assertEqual(playback['text'], 'Verified answer')
        self.assertEqual(playback['source_ref']['original'], 'Verified answer')
        update = {'playback_id': playback['playback_id'], 'client_id': 'phone', 'epoch': 1, 'status': 'completed'}
        with self.assertRaises(ValueError):
            self.turns.update_playback({**update, 'completed_phrases': ['invented']})
        self.turns.update_playback({**update, 'status': 'cancelled', 'completed_phrases': []})
        with self.assertRaises(ValueError):
            self.turns.update_playback({**update, 'completed_phrases': [playback['full_boundary_id']]})
        self.turns.create_playback({**create, 'epoch': 2})
        with self.assertRaises(ValueError):
            self.turns.create_playback(create)
        self.assertEqual(self.effects, [])

    def test_spoken_creation_does_not_render_and_completion_binds_app_cached_rendition(self):
        ref = {'id': 'reply', 'tab': '@1', 'pane': '%1', 'identity': 'fixture', 'label': 'Dog'}
        self.app.jarvis.provider = lambda *a, **k: {'text': 'Original', 'readback': {'original': 'Original', 'reply_ref': ref}}
        receipt = self.app.jarvis.submit('Read it', 'read')
        playback = self.turns.create_playback({'turn_id': receipt['turn_id'], 'client_id': 'phone', 'epoch': 1, 'style': 'spoken'})
        self.assertIsNone(playback['text'])
        self.assertEqual(self.app.readbacks.entries, {})
        update = {'playback_id': playback['playback_id'], 'client_id': 'phone', 'epoch': 1,
                  'status': 'completed', 'completed_phrases': [playback['full_boundary_id']]}
        with self.assertRaises(ValueError):
            self.turns.update_playback(update)
        self.app.readbacks.entries[('Original', 'spoken')] = {'done': True, 'error': None, 'result': {'text': 'App rendition'}}
        completed = self.turns.update_playback(update)
        self.assertEqual(completed['text'], 'App rendition')
        self.assertEqual(self.effects, [ref])


    def test_new_playback_owner_supersedes_old_even_if_cancel_ack_was_lost(self):
        receipt = self.app.jarvis.submit('Hello', 'hello')
        create = {'turn_id': receipt['turn_id'], 'client_id': 'phone', 'epoch': 1, 'style': 'original'}
        old = self.turns.create_playback(create)
        new = self.turns.create_playback({**create, 'client_id': 'desktop', 'epoch': 1})
        with self.assertRaises(ValueError):
            self.turns.update_playback({'playback_id': old['playback_id'], 'client_id': 'phone', 'epoch': 1,
                                      'status': 'completed', 'completed_phrases': [old['full_boundary_id']]})
        self.assertEqual(self.app.jarvis.snapshot()['playbacks'][old['playback_id']]['status'], 'cancelled')
        self.assertEqual(self.app.jarvis.snapshot()['playbacks'][new['playback_id']]['status'], 'generated')
        self.assertEqual(self.effects, [])

    def test_post_dispatch_correction_keeps_original_receipt_and_one_operation(self):
        target = {'tab': '@1', 'pane': '%1', 'identity': 'fixture', 'label': 'Dog Data'}
        sent = []
        self.app.jarvis.authorize = lambda *args: True
        def send(context, specs, execute, **kwargs):
            execute('set_subject', {'target': target})
            draft = execute('prepare_literal', {'start': 15, 'end': 26})
            execute('send_draft', {'draft_id': draft['draft_id']})
            return {'text': 'Submitted'}
        self.app.jarvis.execute_tool = lambda name, args, turn: ({'ok': True, 'status': 'completed', 'subject': target} if name == 'set_subject' else sent.append(args['text']) or {'ok': True, 'status': 'submitted'})
        self.app.jarvis.provider = send
        self.commit(self.turns.decide(self.body()))
        receipt = self.app.jarvis.submit('Tell Dog Data: exact words', 'send', source='voice')
        self.correction = True
        self.turns.decide(self.body('No, tell Controller: revised words', 2, subject=target))
        stored = self.app.jarvis.snapshot()['actions'][receipt['actions'][0]['action_id']]
        self.assertEqual(stored['status'], 'submitted')
        self.assertEqual(stored['payload'], 'exact words')
        self.assertEqual(sent, ['exact words'])

if __name__ == '__main__':
    unittest.main()
