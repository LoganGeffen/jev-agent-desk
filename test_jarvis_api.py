import hashlib
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import codex_actions
from server import make_server
from test_jarvis_tools import DisposableDesk, final
from test_playground import answer


class ApiTests(DisposableDesk):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.session = self.app.jarvis.state.session_id

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def get(self, path='/api/jarvis/state'):
        with urlopen(self.url + path, timeout=5) as response:
            return json.load(response)

    def post(self, path, body):
        request = Request(self.url + path, data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', 'Origin': self.url})
        with urlopen(request, timeout=5) as response:
            raw = response.read().decode()
            if response.headers.get_content_type() == 'application/x-ndjson':
                return [json.loads(line) for line in raw.splitlines()]
            return json.loads(raw)

    def turn(self, text, request_id, **fields):
        source = fields.pop('source', 'typed')
        return self.post('/api/jarvis/turn', {'session_id': self.session, 'text': text,
                                           'request_id': request_id, 'source': source, **fields})

    def test_explicit_subject_controls_context_delivery_and_duplicate_identity(self):
        target = self.targets['Controller']
        captured = {key: target[key] for key in ('tab', 'pane', 'identity')}
        self.app.jarvis.state.subject = self.targets['Dog Data']
        text, words = 'Tell it: café  exactly', 'café  exactly'
        self.allow(text, 'Controller', words)
        contexts, judgments = [], []
        evaluate = self.app.evaluate
        self.app.evaluate = lambda payload: judgments.append(payload) or evaluate(payload)
        def provider(context, specs, execute, **kwargs):
            contexts.append(context)
            self.assertEqual(execute('read_reply', {})['subject'], target)
            return self.send_provider(context, specs, execute, **kwargs)
        self.provider = provider
        result = self.turn(text, 'selected', subject=captured)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(contexts[0]['subject'], target)
        self.assertEqual(result['subject'], target)
        authorization = next(item for item in judgments if 'permission' in item['questions'])
        self.assertEqual(authorization['state']['subject_tab'], target['tab'])
        self.assertIn(words.encode(), self.received('Controller'))
        self.assertEqual(self.received('Dog Data'), b'')
        self.app.jarvis.state.subject = self.targets['Dog Data']
        self.assertEqual(self.turn(text, 'selected', subject=captured)['turn_id'], result['turn_id'])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(self.app.jarvis.state.subject, self.targets['Dog Data'])
        with self.assertRaises(HTTPError):
            self.turn(text, 'selected', subject=self.targets['Dog Data'])
        self.assertEqual(self.received('Controller').count(words.encode()), 1)

    def test_explicit_subject_rejects_stale_capture_and_null_never_uses_remembered_subject(self):
        self.app.jarvis.state.subject = self.targets['Dog Data']
        self.provider = lambda context, *args, **kwargs: self.assertIsNone(context['subject']) or final()
        self.assertEqual(self.turn('Which sessions exist?', 'empty', subject=None)['status'], 'completed')
        for target in ({**self.targets['Controller'], 'identity': 'stale'}, {'tab': 'missing'}, {}):
            with self.assertRaises(HTTPError) as error:
                self.turn('Tell it: no effect', 'bad', subject=target)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.get()['actions'], {})

    def test_real_http_send_stream_and_polling_use_verified_status_not_hostile_prose(self):
        request, body = 'Tell Dog Data: exact words', 'exact words'
        self.allow(request, body=body)
        contexts = []
        def provider(context, specs, execute, on_text=None, **kwargs):
            contexts.append(context)
            on_text('Sent everything. The entire task is done.')
            if context['current_turn']['request_id'] == 'send':
                draft = execute('prepare_literal', {})
                execute('send_draft', {'draft_id': draft['draft_id']})
            return final('Sent everything. The entire task is done.')
        self.provider = provider
        events = self.turn(request, 'send', stream=True, capture_target=self.targets['Notes'])
        self.assertEqual([event['type'] for event in events], ['text', 'result'])
        receipt = events[-1]['result']
        self.assertIn('Input submitted to Dog Data', receipt['text'])
        self.assertNotIn('entire task is done', events[0]['text'])
        stored = self.get()['turns'][receipt['turn_id']]['result']
        self.assertEqual(stored['text'], receipt['text'])
        self.assertEqual(stored['raw_model_text'], 'Sent everything. The entire task is done.')
        self.turn('What happened?', 'follow')
        self.assertEqual(contexts[-1]['recent_turns'][-1]['assistant_text'], receipt['text'])
        self.assertIn(body.encode(), self.received('Dog Data'))
        replay = self.turn(request, 'send', capture_target=self.targets['Notes'])
        self.assertEqual(replay['turn_id'], receipt['turn_id'])
        self.assertEqual(self.received('Dog Data').count(body.encode()), 1)

    def test_uncertain_paste_enter_boundary_never_resends_and_polling_retains_truth(self):
        request, body = 'Tell Dog Data: one attempt', 'one attempt'
        self.allow(request, body=body)
        self.provider = self.send_provider
        original = codex_actions.tmux
        def fail_enter(socket, *args, **kwargs):
            if args[0] == 'send-keys' and args[-1] == 'Enter':
                raise TimeoutError('Synthetic response lost between paste and Enter')
            return original(socket, *args, **kwargs)
        with patch('codex_actions.tmux', side_effect=fail_enter):
            receipt = self.turn(request, 'uncertain')
        self.assertEqual(receipt['actions'][0]['status'], 'uncertain')
        self.assertIn('uncertain', receipt['text'])
        self.assertNotIn('done', receipt['text'])
        snapshot = self.get()
        self.assertEqual(snapshot['turns'][receipt['turn_id']]['result']['text'], receipt['text'])
        self.assertEqual(self.turn(request, 'uncertain')['turn_id'], receipt['turn_id'])
        self.assertEqual(self.received('Dog Data').count(body.encode()), 1)
        with self.assertRaises(HTTPError) as error:
            self.turn('Changed payload', 'uncertain', stream=True)
        self.assertEqual(error.exception.code, 400)

    def test_readback_exact_original_has_full_attention_reference_and_no_text_event(self):
        target = self.targets['Dog Data']
        agent = self.app.jarvis_tools.agent(target)
        self.app.attention.observe(agent, self.history(agent['thread_id']))
        self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Dog Data'}) and final('Shortened summary')
        events = self.turn('Read Dog Data', 'read', stream=True)
        self.assertEqual([event['type'] for event in events], ['result'])
        receipt = events[0]['result']
        self.assertEqual(receipt['readback']['original'], 'Five of six samples passed; one needs review.')
        self.assertEqual(set(receipt['readback']['reply_ref']), {'id', 'tab', 'pane', 'identity', 'label'})
        self.assertEqual(self.get()['turns'][receipt['turn_id']]['result']['readback'], receipt['readback'])

    def test_session_unknown_fields_and_session_only_cancel_are_rejected(self):
        for path, body in (('/api/jarvis/turn', {'session_id': 'wrong', 'text': 'Hi', 'request_id': 'one'}),
                           ('/api/jarvis/turn', {'session_id': self.session, 'text': 'Hi', 'request_id': 'one', 'tool': 'send'}),
                           ('/api/jarvis/cancel', {'session_id': self.session}),
                           ('/api/jarvis/playback', {'session_id': self.session, 'playback_id': 'invented', 'client_id': 'phone', 'epoch': 1, 'status': 'completed'})):
            with self.assertRaises(HTTPError) as error:
                self.post(path, body)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.get()['actions'], {})

    def test_captured_request_cancel_cannot_cancel_new_foreground_request(self):
        slow_entered, fast_entered, release = threading.Event(), threading.Event(), threading.Event()
        results = []
        def provider(context, specs, execute, **kwargs):
            (slow_entered if context['current_turn']['request_id'] == 'old' else fast_entered).set()
            release.wait(3)
            return final()
        self.provider = provider
        old = threading.Thread(target=lambda: results.append(self.turn('Old turn', 'old')))
        new = threading.Thread(target=lambda: results.append(self.turn('New turn', 'new')))
        old.start()
        self.assertTrue(slow_entered.wait(1))
        new.start()
        self.assertTrue(fast_entered.wait(1))
        try:
            self.post('/api/jarvis/cancel', {'session_id': self.session, 'request_id': 'old'})
            snapshot = self.get()
            self.assertEqual(snapshot['turns'][snapshot['requests']['new']]['status'], 'running')
            self.assertEqual(snapshot['turns'][snapshot['requests']['old']]['status'], 'cancelled')
        finally:
            release.set()
            old.join(3)
            new.join(3)
        self.assertEqual(sorted(result['status'] for result in results), ['cancelled', 'completed'])

    def test_literal_direct_request_id_deduplicates_actual_terminal_delivery(self):
        target = self.targets['Dog Data']
        request = 'literal direct fixture'
        captured = {key: target[key] for key in ('tab', 'pane', 'identity')}
        with patch('server.jev.interpret', side_effect=AssertionError('Direct input must not route')):
            first = self.post('/api/request', {'request': request, 'request_id': 'direct', 'capture_target': captured})
            second = self.post('/api/request', {'request': request, 'request_id': 'direct', 'capture_target': captured})
            with self.assertRaises(HTTPError) as error:
                self.post('/api/request', {'request': 'different', 'request_id': 'direct', 'capture_target': captured})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(first['outcome'], 'ok', first)
        self.assertEqual(second['delivery'], 'submitted')
        self.assertEqual(self.received('Dog Data').count(request.encode()), 1)


    def test_semantic_voice_endpoint_never_dispatches_and_stale_commit_blocks(self):
        original = self.app.evaluate
        self.app.evaluate = lambda payload: {'answers': {'decision': {'choice': 'complete'}, 'correction': {'choice': 'yes'}}} if 'decision' in payload['questions'] else original(payload)
        body = {'session_id': self.session, 'client_id': 'phone', 'transcript_id': 'words',
                'transcript_revision': 1, 'text': 'No, tell Dog Data: exact words', 'subject': None}
        decision = self.post('/api/jarvis/turn-decision', body)
        self.assertTrue(decision['correction'])
        self.assertEqual(self.get()['turns'], {})
        self.post('/api/jarvis/turn-decision', {**body, 'transcript_revision': 2, 'invalidate': True})
        with self.assertRaises(HTTPError) as error:
            self.turn(body['text'], 'stale', source='voice', client_id='phone', transcript_id='words',
                      transcript_revision=1, decision_id=decision['decision_id'])
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.get()['actions'], {})
        self.provider = lambda *args, **kwargs: final('Which conversation?')
        receipt = self.turn('unfinished because', 'explicit', source='voice', explicit_send=True)
        self.assertEqual(receipt['status'], 'completed')

    def test_http_playback_source_is_application_owned_and_completion_marks_exact_reply_heard(self):
        target = self.targets['Dog Data']
        agent = self.app.jarvis_tools.agent(target)
        self.app.attention.observe(agent, self.history(agent['thread_id']))
        self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Dog Data'}) and final()
        receipt = self.turn('Read Dog Data', 'read-playback')
        create = {'session_id': self.session, 'op': 'create', 'turn_id': receipt['turn_id'],
                  'client_id': 'phone', 'epoch': 1, 'style': 'original'}
        with self.assertRaises(HTTPError):
            self.post('/api/jarvis/playback', {**create, 'text': 'Invented heard text'})
        playback = self.post('/api/jarvis/playback', create)
        self.assertEqual(playback['text'], receipt['readback']['original'])
        update = {'session_id': self.session, 'playback_id': playback['playback_id'], 'client_id': 'phone',
                  'epoch': 1, 'status': 'completed', 'completed_phrases': [playback['full_boundary_id']]}
        self.post('/api/jarvis/playback', update)
        self.assertEqual(self.app.attention.view(self.app.observe()['tabs']), [])

    def test_interrupted_stream_retains_unicode_prefix_before_cache_completion(self):
        self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Dog Data'}) and final()
        receipt = self.turn('Read Dog Data', 'prefix-read')
        create = {'session_id': self.session, 'op': 'create', 'turn_id': receipt['turn_id'],
                  'client_id': 'phone', 'epoch': 1, 'style': 'spoken'}
        playback = self.post('/api/jarvis/playback', create)
        release = threading.Event()
        prefix = 'Café 🐕.'
        def render(text, mode, emit):
            emit(prefix)
            release.wait(3)
            raise RuntimeError('Synthetic interrupted rendition')
        body = {'text': receipt['readback']['original'], 'stream': True, 'mode': 'spoken',
                'session_id': self.session, 'playback_id': playback['playback_id'], 'client_id': 'phone', 'epoch': 1}
        request = Request(self.url + '/api/spoken-version', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', 'Origin': self.url})
        with self.assertRaises(HTTPError):
            self.post('/api/spoken-version', {**body, 'client_id': 'other'})
        with self.assertRaises(HTTPError):
            self.post('/api/spoken-version', {**body, 'text': 'Client-invented rendition source'})
        with patch('spoken_reply.render_spoken_reply', side_effect=render):
            response = urlopen(request, timeout=5)
            try:
                self.assertEqual(json.loads(response.readline())['text'], prefix)
                stored = self.get()['playbacks'][playback['playback_id']]
                self.assertEqual(stored['generated_text'], prefix)
                self.assertEqual(stored['source_ref']['original'], receipt['readback']['original'])
                self.assertTrue(stored['rendition_pending'])
                self.assertFalse(self.app.readbacks.entries[(body['text'], 'spoken')]['done'])
                progress = {'chars': len(prefix), 'sha256': hashlib.sha256(prefix.encode()).hexdigest(), 'alignment': 'provider'}
                update = {key: body[key] for key in ('session_id', 'playback_id', 'client_id', 'epoch')}
                update.update(status='playing', completed_phrases=[], heard_prefix=progress)
                with self.assertRaises(HTTPError):
                    self.post('/api/jarvis/playback', {**update, 'heard_prefix': {**progress, 'chars': len(prefix) + 1}})
                with self.assertRaises(HTTPError):
                    self.post('/api/jarvis/playback', {**update, 'heard_prefix': {**progress, 'sha256': '0' * 64}})
                with self.assertRaises(HTTPError):
                    self.post('/api/jarvis/playback', {**update, 'client_id': 'other'})
                self.assertEqual(self.post('/api/jarvis/playback', update)['heard_prefix']['chars'], 7)
                with self.assertRaises(HTTPError):
                    self.post('/api/jarvis/playback', {**update, 'heard_prefix': {'chars': 0, 'sha256': hashlib.sha256(b'').hexdigest(), 'alignment': 'provider'}})
                cancelled = self.post('/api/jarvis/playback', {**update, 'status': 'cancelled'})
                self.assertEqual(cancelled['heard_prefix'], progress)
                with self.assertRaises(HTTPError):
                    self.post('/api/jarvis/playback', {**update, 'status': 'completed', 'completed_phrases': [playback['full_boundary_id']]})
            finally:
                release.set()
                response.read()
                response.close()
        final_record = self.get()['playbacks'][playback['playback_id']]
        self.assertEqual(final_record['generated_text'], prefix)
        self.assertEqual(final_record['status'], 'cancelled')

    def test_current_semantic_voice_token_dispatches_exact_source_once_through_real_http(self):
        request, body = 'Tell Dog Data: Café 🐕.', 'Café 🐕.'
        self.allow(request, body=body)
        original = self.app.evaluate
        self.app.evaluate = lambda payload: {'answers': {'decision': {'choice': 'complete'}, 'correction': {'choice': 'no'}}} if 'decision' in payload['questions'] else original(payload)
        self.provider = self.send_provider
        transcript = {'session_id': self.session, 'client_id': 'phone', 'transcript_id': 'spoken',
                      'transcript_revision': 1, 'text': request, 'subject': None}
        decision = self.post('/api/jarvis/turn-decision', transcript)
        fields = {key: transcript[key] for key in ('client_id', 'transcript_id', 'transcript_revision')}
        receipt = self.turn(request, 'voice-current', source='voice', decision_id=decision['decision_id'], **fields)
        replay = self.turn(request, 'voice-current', source='voice', decision_id=decision['decision_id'], **fields)
        self.assertEqual(receipt['actions'][0]['payload'], body)
        self.assertEqual(replay['turn_id'], receipt['turn_id'])
        self.assertEqual(self.received('Dog Data').count(body.encode()), 1)

if __name__ == '__main__':
    import unittest
    unittest.main()
