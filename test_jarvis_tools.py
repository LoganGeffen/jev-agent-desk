import ctypes
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_actions import tmux
from jarvis_state import JarvisState
from server import Playground


def install_m4_state_helpers():
    # Mirrors the frozen M4-API state helpers until the durability baseline provides them.
    import copy
    if hasattr(JarvisState, 'update_job'):
        return
    original_snapshot, original_init = JarvisState.snapshot, JarvisState.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.notices, self.attention = {}, None

    def snapshot(self):
        with self.lock:
            return dict(original_snapshot(self), notices=copy.deepcopy(self.notices), attention=copy.deepcopy(self.attention))

    def update_job(self, job_id, status, result=None):
        with self.lock:
            job = self.jobs[job_id]
            if job['status'] in ('completed', 'failed', 'interrupted'):
                return copy.deepcopy(job)
            job.update(status=status, result=copy.deepcopy(result) if result is not None else job['result'])
            self.changed()
            return copy.deepcopy(job)

    def record_notice(self, notice):
        with self.lock:
            self.notices.setdefault(notice['id'], copy.deepcopy(notice))
            self.changed()
            return copy.deepcopy(self.notices[notice['id']])

    def set_attention(self, value):
        with self.lock:
            self.attention = copy.deepcopy(value)
            self.changed()

    def records(self, ids):
        found = [self.drafts.get(item) or self.clarifications.get(item) for item in ids]
        if any(record is None or record['status'] not in ('prepared', 'pending', 'needs_review') for record in found):
            raise ValueError('Only unsent retained records can be offered')
        return found

    def offer_pending(self, turn_id, ids, question):
        with self.lock:
            for record in records(self, ids):
                record.update(prompt_turn_id=turn_id, prompt=question)
            self.changed()

    def review_pending(self, turn_id, ids):
        with self.lock:
            records(self, ids)
            self.changed()

    def discard_pending(self, turn_id, ids):
        with self.lock:
            for record in records(self, ids):
                record['status'] = 'discarded'
            self.changed()

    original_confirm = JarvisState.confirm_draft

    def confirm_draft(self, turn_id, target, draft_id=None, clarification_id=None):
        with self.lock:
            record = self.drafts.get(draft_id) or self.clarifications.get(clarification_id)
            turns = list(self.turns)
            if record is None or record.get('prompt_turn_id') != turns[turns.index(turn_id) - 1]:
                raise ValueError('Retained record was not offered by the immediately previous turn')
            return original_confirm(self, turn_id, target, draft_id, clarification_id)

    for function in (init, snapshot, update_job, record_notice, set_attention, offer_pending, review_pending, discard_pending, confirm_draft):
        setattr(JarvisState, '__init__' if function is init else function.__name__, function)


install_m4_state_helpers()


def final(text='Untrusted model claims sent and done.'):
    return {'text': text, 'model': 'synthetic', 'first_text_ms': 0, 'elapsed_ms': 1}


class DisposableDesk(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='jarvis-tools-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.socket = str(self.root / 'tmux.sock')
        fixture = self.root / 'fixture.py'
        fixture.write_text("import ctypes, os, sys, tty\nctypes.CDLL(None).prctl(15, b'codex', 0, 0, 0)\ntty.setraw(0)\nprint('\\x1b[?2004h', end='', flush=True)\nwhile True:\n data=os.read(0, 65536)\n with open(sys.argv[1], 'ab') as f: f.write(data)\n")
        for number, name in enumerate(('Dog Data', 'Controller', 'Notes')):
            command = 'exec -a codex ' + shlex.join([sys.executable, str(fixture), str(self.root / (name.replace(' ', '-') + '.input'))])
            if number == 0:
                tmux(self.socket, '-f', '/dev/null', 'new-session', '-d', '-s', 'tabs', '-n', name, command)
                self.addCleanup(tmux, self.socket, 'kill-server')
            else:
                tmux(self.socket, 'new-window', '-d', '-t', 'tabs:', '-n', name, command)
        self.intents = {}
        self.provider = lambda *args, **kwargs: final('No tools.')
        def identity(pid, **kwargs):
            stat = Path('/proc') / str(pid) / 'stat'
            return {'process': str(pid) + ':' + stat.read_text().split()[21],
                    'thread_id': 'fixture-' + str(pid), 'tracking': 'synthetic'}
        for target in ('server.process_identity', 'codex_actions.process_identity'):
            mocked = patch(target, side_effect=identity)
            mocked.start()
            self.addCleanup(mocked.stop)
        deadline = time.monotonic() + 3
        while 'codex' not in tmux(self.socket, 'list-panes', '-a', '-F', '#{pane_current_command}'):
            if time.monotonic() > deadline:
                raise AssertionError('Disposable synthetic command did not start: ' + tmux(self.socket, 'list-panes', '-a', '-F', '#{pane_current_command}'))
            time.sleep(.02)
        self.app = Playground(self.socket, self.root / 'events.jsonl', self.evaluate,
                              independent_selection=True, jarvis_provider=lambda *args, **kwargs: self.provider(*args, **kwargs))
        watcher = patch.object(self.app.reply_watch, 'start')
        watcher.start()
        self.addCleanup(watcher.stop)
        self.targets = {item['label']: {key: value for key, value in item.items() if key != 'number'}
                        for item in self.app.jarvis_tools.sessions()}
        self.assertEqual(set(self.targets), {'Dog Data', 'Controller', 'Notes'})
        def history(thread):
            return {'turns': [{'id': 'reply-' + thread, 'status': 'completed', 'items': [
                {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Five of six samples passed; one needs review.'}]}]}
        for target, callback in (('codex_actions.thread_read', history), ('server.reply_history', lambda agent: history(agent['thread_id']))):
            mocked = patch(target, side_effect=callback)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.history = history

    def evaluate(self, payload):
        if 'narrative_safety' in payload['questions']:
            return {'answers': {'narrative_safety': {'choice': 'safe' if getattr(self, 'narrative_safe', False) else 'unproven_effect'}}}
        if 'permission' not in payload['questions']:
            return {'answers': {key: {'choice': 'reply'} for key in payload['questions']}}
        request = payload['state']['request']
        decision = self.intents.get(request, {})
        target = decision.get('target')
        target_choice = target['identity'] if 'pending_message' in payload['state'] and target else target['tab'] if target else 'none'
        answers = {'permission': {'choice': decision.get('permission', 'deny')}, 'target': {'choice': target_choice},
                   'message_start': {'choice': str(decision.get('start', 0))},
                   'message_end': {'choice': str(decision.get('end', len(request)))},
                   'message_form': {'choice': decision.get('form', 'verbatim')}}
        if target:
            answers['pane:' + target['tab']] = {'choice': target['pane']}
        return {'model': 'synthetic-jev', 'answers': answers}

    def allow(self, request, target='Dog Data', body=None):
        body = request if body is None else body
        self.intents[request] = {'permission': 'allow', 'target': self.targets.get(target),
                                 'start': request.index(body), 'end': request.index(body) + len(body)}

    def send_provider(self, context, specs, execute, **kwargs):
        draft = execute('prepare_literal', {})
        if draft.get('draft_id'):
            execute('send_draft', {'draft_id': draft['draft_id']})
        return final()

    def received(self, name):
        file = self.root / (name.replace(' ', '-') + '.input')
        deadline = time.monotonic() + 2
        while not file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        return file.read_bytes() if file.exists() else b''


class ToolTests(DisposableDesk):
    def test_real_adapter_sends_exact_body_and_keeps_visible_notes_selection(self):
        request, body = 'Tell Dog Data: café\n x = 1;  ', 'café\n x = 1;  '
        self.allow(request, body=body)
        self.app.focus(self.targets['Notes']['tab'])
        self.provider = self.send_provider
        receipt = self.app.jarvis.submit(request, 'send', capture_target=self.targets['Notes'])
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(receipt['actions'][0]['payload'], body)
        self.assertEqual(receipt['actions'][0]['status'], 'submitted')
        terminal_body = body.replace('\n', '\r').encode()
        self.assertIn(terminal_body, self.received('Dog Data'))
        self.assertEqual(self.received('Dog Data').count(terminal_body), 1)
        self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])
        self.assertEqual(self.app.jarvis.snapshot()['subject'], self.targets['Dog Data'])
        self.assertNotIn('done', receipt['text'])
        self.assertIn('not complete', receipt['text'])

    def test_hostile_dog_draft_cannot_satisfy_explicit_controller_intent(self):
        request, body = 'Tell Controller: pause the queue', 'pause the queue'
        self.allow(request, 'Controller', body)
        def provider(context, specs, execute, **kwargs):
            execute('resolve_subject', {'name': 'Dog Data'})
            draft = self.app.jarvis.state.prepare_literal(context['current_turn']['turn_id'], request.index(body), len(request))
            result = execute('send_draft', {'draft_id': draft['draft_id']})
            self.assertEqual(result['status'], 'not_attempted')
            return final('Sent to Controller. All done.')
        self.provider = provider
        receipt = self.app.jarvis.submit(request, 'one')
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(receipt['actions'], [])
        self.assertIn('No message was submitted', receipt['text'])
        self.assertEqual(self.received('Dog Data'), b'')
        self.assertEqual(self.received('Controller'), b'')

    def test_read_keeps_exact_original_and_does_not_focus(self):
        self.app.focus(self.targets['Notes']['tab'])
        self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Dog Data'}) and final('Summary only.')
        receipt = self.app.jarvis.submit('Read Dog Data', 'read')
        self.assertEqual(receipt['text'], 'Five of six samples passed; one needs review.')
        self.assertEqual(receipt['readback']['original'], receipt['text'])
        self.assertEqual(receipt['readback']['target'], self.targets['Dog Data'])
        self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])

    def test_identity_changes_before_send_block_delivery_and_false_model_success(self):
        request = 'Tell Dog Data: unchanged text'
        self.allow(request, body='unchanged text')
        def provider(context, specs, execute, **kwargs):
            draft = execute('prepare_literal', {})
            with patch.object(self.app, 'validate_pane', side_effect=ValueError('Conversation replaced')):
                execute('send_draft', {'draft_id': draft['draft_id']})
            return final()
        self.provider = provider
        receipt = self.app.jarvis.submit(request, 'changed')
        self.assertEqual(receipt['status'], 'completed')
        self.assertIn('No message was submitted', receipt['text'])
        self.assertIn('Conversation replaced', receipt['text'])
        self.assertNotIn('not authorized', receipt['text'])
        self.assertEqual(self.received('Dog Data'), b'')

    def retain_draft(self, request='Tell Dog Data: preserve this exact body', body='preserve this exact body'):
        self.allow(request, body=body)
        drafts = []
        self.provider = lambda context, specs, execute, **kwargs: drafts.append(execute('prepare_literal', {})['draft_id']) or final('Draft prepared.')
        return self.app.jarvis.submit(request, 'draft-' + body), drafts[0]

    def test_retained_draft_requires_explicit_review_then_confirmation_and_preserves_source(self):
        first, draft_id = self.retain_draft()
        source_turn = first['turn_id']
        self.provider = lambda context, specs, execute, **kwargs: execute('review_pending', {'record_id': draft_id}) and final('Reviewed.')
        review = self.app.jarvis.submit('Go back to that Dog Data message', 'review-2')
        self.assertEqual(review['text'], 'Send this exact message to Dog Data? preserve this exact body')
        self.assertEqual(self.app.jarvis.snapshot()['drafts'][draft_id]['prompt_turn_id'], review['turn_id'])
        payloads = []
        evaluate = self.app.evaluate
        self.app.evaluate = lambda payload: payloads.append(payload) or evaluate(payload)
        self.intents['yes, Dog Data'] = {'permission': 'allow', 'target': self.targets['Dog Data']}
        def confirmed(context, specs, execute, **kwargs):
            draft = execute('confirm_draft', {'draft_id': draft_id})
            execute('send_draft', {'draft_id': draft['draft_id']})
            return final()
        self.provider = confirmed
        receipt = self.app.jarvis.submit('yes, Dog Data', 'confirm')
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(receipt['actions'][0]['payload'], 'preserve this exact body')
        sent_draft = self.app.jarvis.snapshot()['drafts'][receipt['actions'][0]['draft_id']]
        self.assertEqual(sent_draft['source_turn_id'], source_turn)
        self.assertIn(b'preserve this exact body', self.received('Dog Data'))
        offer = next(payload['state']['offer'] for payload in payloads if 'offer' in payload.get('state', {}))
        self.assertEqual(offer['prompt'], review['text'])
        self.assertTrue(offer['prompt_was_immediately_previous_turn'])
        self.assertEqual(offer['source_user_text'], 'Tell Dog Data: preserve this exact body')

    def test_unrelated_bare_yes_cannot_resurrect_forgotten_or_stale_offered_draft(self):
        _, draft_id = self.retain_draft()
        calls = []
        def attempt(context, specs, execute, **kwargs):
            result = execute('confirm_draft', {'draft_id': draft_id})
            calls.append(result)
            return final()
        self.intents['yes'] = {'permission': 'allow', 'target': self.targets['Dog Data']}
        self.provider = attempt
        forgotten = self.app.jarvis.submit('yes', 'forgotten')
        self.assertEqual(calls[-1]['status'], 'not_attempted')
        self.assertIn('not offered', forgotten['text'])
        self.provider = lambda context, specs, execute, **kwargs: execute('review_pending', {'record_id': draft_id}) and final()
        self.app.jarvis.submit('Show me that unsent message', 'offer')
        self.provider = lambda *args, **kwargs: final('Unrelated.')
        self.app.jarvis.submit('What is two plus two?', 'intervening')
        payloads = []
        evaluate = self.app.evaluate
        self.app.evaluate = lambda payload: payloads.append(payload) or evaluate(payload)
        self.provider = attempt
        stale = self.app.jarvis.submit('yes', 'stale-yes')
        self.assertNotEqual(self.app.jarvis.snapshot()['drafts'][draft_id]['status'], 'submitted')
        self.assertEqual(self.app.jarvis.snapshot()['actions'], {})
        self.assertEqual(stale['actions'], [])
        self.assertEqual(self.received('Dog Data'), b'')
        self.assertFalse(any(payload['state']['offer']['prompt_was_immediately_previous_turn'] for payload in payloads if 'offer' in payload.get('state', {})))

    def test_pending_list_is_bounded_and_discard_requires_explicit_user_request_and_keeps_sent_records(self):
        self.retain_draft()
        listed = {}
        self.provider = lambda context, specs, execute, **kwargs: listed.update(execute('list_pending', {})) or final('Listed.')
        self.app.jarvis.submit('What is unsent?', 'list')
        self.assertEqual([item['text'] for item in listed['pending']], ['preserve this exact body'])
        record_id = listed['pending'][0]['record_id']
        results = []
        self.provider = lambda context, specs, execute, **kwargs: results.append(execute('discard_pending', {'record_ids': [record_id]})) or final()
        self.app.jarvis.submit('Mention that draft', 'no-discard')
        self.assertEqual(results[-1]['status'], 'not_attempted')
        self.assertEqual(self.app.jarvis.snapshot()['drafts'][record_id]['status'], 'prepared')
        self.intents['Forget that unsent message'] = {'permission': 'allow'}
        self.app.jarvis.submit('Forget that unsent message', 'discard')
        self.assertEqual(results[-1]['status'], 'completed')
        self.assertEqual(self.app.jarvis.snapshot()['drafts'][record_id]['status'], 'discarded')
        self.assertEqual(self.received('Dog Data'), b'')

    def test_tool_evidence_allows_checked_harmless_synthesis_but_blocks_false_claims(self):
        self.provider = lambda context, specs, execute, **kwargs: execute('list_sessions', {}) and final('Three conversations are open: Dog Data, Controller and Notes.')
        payloads = []
        evaluate = self.app.evaluate
        self.app.evaluate = lambda payload: payloads.append(payload) or evaluate(payload)
        self.narrative_safe = True
        receipt = self.app.jarvis.submit('Which chats are open?', 'harmless')
        self.assertEqual(receipt['text'], 'Three conversations are open: Dog Data, Controller and Notes.')
        evidence = payloads[-1]['state']['observed_evidence']
        self.assertEqual(evidence[0]['tool'], 'list_sessions')
        self.assertIn('Dog Data', evidence[0]['output'])
        self.assertEqual(payloads[-1]['state']['proven_actions'], [])
        self.narrative_safe = False
        self.provider = lambda context, specs, execute, **kwargs: execute('list_sessions', {}) and final('I sent it to Dog Data and it is done.')
        claimed = self.app.jarvis.submit('Which chats are open now?', 'claim')
        self.assertNotIn('sent', claimed['text'])
        self.assertIn('Dog Data', claimed['text'])
        self.narrative_safe = True
        with patch.object(self.app, 'evaluate', side_effect=RuntimeError('Synthetic judgment unavailable')):
            held = self.app.jarvis.submit('And again?', 'unavailable')
        self.assertIn('Dog Data', held['text'])

    def test_unresolved_recipient_clarification_retains_exact_body(self):
        request = 'Tell it: retained words'
        self.allow(request, target='missing', body='retained words')
        self.provider = self.send_provider
        initial = self.app.jarvis.submit(request, 'pending')
        self.assertIn('Which conversation', initial['text'])
        self.assertNotIn('unauthorized', initial['text'])
        pending = next(iter(self.app.jarvis.snapshot()['clarifications'].values()))
        self.assertEqual(pending['text'], 'retained words')
        self.assertEqual((pending['prompt_turn_id'], pending['prompt']), (initial['turn_id'], initial['text']))
        self.intents['yes, Dog Data'] = {'permission': 'allow', 'target': self.targets['Dog Data']}
        def confirmed(context, specs, execute, **kwargs):
            draft = execute('confirm_draft', {'clarification_id': pending['clarification_id']})
            execute('send_draft', {'draft_id': draft['draft_id']})
            return final()
        self.provider = confirmed
        receipt = self.app.jarvis.submit('yes, Dog Data', 'resolved')
        self.assertEqual(receipt['actions'][0]['payload'], 'retained words')
        self.assertIn(b'retained words', self.received('Dog Data'))

    def test_harmless_no_tool_conversation_survives_claim_check_and_failure_holds(self):
        self.narrative_safe = True
        self.provider = lambda *args, **kwargs: final('Four. Would you like an example?')
        receipt = self.app.jarvis.submit('What is two plus two?', 'math')
        self.assertEqual(receipt['text'], 'Four. Would you like an example?')
        self.assertEqual(self.app.jarvis.snapshot()['turns'][receipt['turn_id']]['result']['text'], receipt['text'])
        self.provider = lambda *args, **kwargs: final('Which conversation do you mean?')
        with patch.object(self.app, 'evaluate', side_effect=RuntimeError('Synthetic judgment unavailable')):
            held = self.app.jarvis.submit('Can you clarify?', 'failed-check')
        self.assertEqual(held['text'], 'No workspace action was performed.')

    def test_no_tool_false_completion_never_becomes_conversation_truth(self):
        self.narrative_safe = False
        contexts = []
        def provider(context, *args, **kwargs):
            contexts.append(context)
            return final('Sent to Controller. Its task is done.')
        self.provider = provider
        first = self.app.jarvis.submit('Hello', 'first')
        self.assertEqual(first['text'], 'No workspace action was performed.')
        self.app.jarvis.submit('And now?', 'second')
        self.assertEqual(contexts[1]['recent_turns'][-1]['assistant_text'], first['text'])
        self.assertNotIn('task is done', contexts[1]['recent_turns'][-1]['assistant_text'])

    def test_exact_name_wins_over_longer_similar_name(self):
        tmux(self.socket, 'rename-window', '-t', self.targets['Notes']['tab'], 'Dog Data 2')
        self.assertEqual(self.app.jarvis_tools.target('Dog Data'), self.targets['Dog Data'])

    def test_observer_job_is_quiet_and_follow_up_remains_responsive(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {'text': 'Observer evidence', 'snapshot': {'observed_at': 'now'}}
        self.provider = lambda context, specs, execute, **kwargs: execute('observer_job', {'name': 'Dog Data'}) and final()
        with patch('jarvis_tools.ask_session', side_effect=slow):
            first = self.app.jarvis.submit('What is Dog Data doing?', 'job')
            self.assertTrue(entered.wait(1))
            job_id = next(iter(self.app.jarvis.snapshot()['jobs']))
            self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Controller'}) and final()
            second = self.app.jarvis.submit('While it runs read Controller', 'follow')
            self.assertEqual(second['status'], 'completed')
            self.assertEqual(self.app.jarvis.snapshot()['jobs'][job_id]['status'], 'running')
            self.assertEqual(self.app.jarvis.snapshot()['subject'], self.targets['Controller'])
            release.set()
        self.assertNotIn('sent', first['text'])


    def test_post_submission_bookkeeping_failure_preserves_receipt_and_blocks_reprepared_resend(self):
        for failure in ('reply_watch', 'attention'):
            with self.subTest(failure=failure):
                request, body = 'Tell Dog Data: only once ' + failure, 'only once ' + failure
                self.allow(request, body=body)
                results = []
                def provider(context, specs, execute, **kwargs):
                    first = execute('prepare_literal', {})
                    results.append(execute('send_draft', {'draft_id': first['draft_id']}))
                    boundary = self.app.jarvis.snapshot()['turns'][context['current_turn']['turn_id']]['literal_boundaries'][0]['boundary_id']
                    again = execute('prepare_literal', {'boundary_id': boundary})
                    results.append(execute('send_draft', {'draft_id': again['draft_id']}))
                    return final('Submission failed. Try sending again.')
                self.provider = provider
                target = self.app.reply_watch if failure == 'reply_watch' else self.app.attention
                method = 'start' if failure == 'reply_watch' else 'answered'
                with patch.object(target, method, side_effect=RuntimeError('Synthetic bookkeeping failure')):
                    receipt = self.app.jarvis.submit(request, failure)
                self.assertEqual([result['status'] for result in results], ['submitted', 'submitted'])
                self.assertTrue(results[-1]['replayed'])
                self.assertIn('Post-submission bookkeeping failed', results[0]['warning'])
                self.assertEqual(results[-1]['warning'], results[0]['warning'])
                self.assertEqual(len(receipt['actions']), 1)
                self.assertEqual(receipt['actions'][0]['status'], 'submitted')
                self.assertIn('Input submitted to Dog Data', receipt['text'])
                self.assertNotIn('failed', receipt['text'])
                self.assertEqual(self.received('Dog Data').count(body.encode()), 1)

if __name__ == '__main__':
    unittest.main()
