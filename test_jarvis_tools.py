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
from server import Playground


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
        self.assertEqual(self.received('Dog Data'), b'')

    def test_retained_draft_requires_explicit_confirmation_and_preserves_source(self):
        request = 'Tell Dog Data: preserve this exact body'
        self.allow(request, body='preserve this exact body')
        drafts = []
        self.provider = lambda context, specs, execute, **kwargs: drafts.append(execute('prepare_literal', {})['draft_id']) or final('Draft prepared.')
        first = self.app.jarvis.submit(request, 'draft')
        source_turn = first['turn_id']
        self.intents['yes, Dog Data'] = {'permission': 'allow', 'target': self.targets['Dog Data']}
        def confirmed(context, specs, execute, **kwargs):
            draft = execute('confirm_draft', {'draft_id': drafts[0]})
            execute('send_draft', {'draft_id': draft['draft_id']})
            return final()
        self.provider = confirmed
        receipt = self.app.jarvis.submit('yes, Dog Data', 'confirm')
        self.assertEqual(receipt['status'], 'completed', receipt)
        self.assertEqual(receipt['actions'][0]['payload'], 'preserve this exact body')
        sent_draft = self.app.jarvis.snapshot()['drafts'][receipt['actions'][0]['draft_id']]
        self.assertEqual(sent_draft['source_turn_id'], source_turn)
        self.assertIn(b'preserve this exact body', self.received('Dog Data'))

    def test_unresolved_recipient_clarification_retains_exact_body(self):
        request = 'Tell it: retained words'
        self.allow(request, target='missing', body='retained words')
        self.provider = self.send_provider
        self.app.jarvis.submit(request, 'pending')
        pending = next(iter(self.app.jarvis.snapshot()['clarifications'].values()))
        self.assertEqual(pending['text'], 'retained words')
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


if __name__ == '__main__':
    unittest.main()
