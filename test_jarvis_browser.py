import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from server import make_server
from test_jarvis_tools import DisposableDesk, final


class RealBrowserTests(DisposableDesk):
    def test_mobile_page_uses_actual_http_controller_and_terminal_delivery(self):
        if not Path(__file__).with_name('jarvis.js').exists():
            self.skipTest('Frontend commit must be integrated for the real-page gate')
        request = 'Ask it to explain those numbers with an example'
        body = 'explain those numbers with an example'
        self.allow(request, body=body)
        contexts = []
        def provider(context, specs, execute, **kwargs):
            contexts.append(context)
            text = context['current_turn']['user_text']
            if text == 'What is ready?':
                execute('list_ready', {})
            elif text == 'Read Dog Data':
                execute('read_reply', {'name': 'Dog Data'})
            elif text == request:
                draft = execute('prepare_literal', {})
                execute('send_draft', {'draft_id': draft['draft_id']})
            else:
                execute('read_reply', {'name': 'Controller'})
            return final('Untrusted model: sent and all done.')
        self.provider = provider
        for name in ('Dog Data', 'Controller'):
            agent = self.app.jarvis_tools.agent(self.targets[name])
            self.app.attention.observe(agent, self.history(agent['thread_id']))
        self.app.focus(self.targets['Notes']['tab'])
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors, scripts, requests = [], [], []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: scripts.append(response.status) if response.url.endswith('/jarvis.js') else None)
                page.on('request', lambda req: requests.append(req.post_data_json) if req.url.endswith('/api/jarvis/turn') else None)
                page.goto('http://127.0.0.1:' + str(server.server_port))
                page.wait_for_function('currentState !== null && jarvis.sessionId !== null')
                self.assertEqual(page.locator('#mode-jarvis').get_attribute('aria-pressed'), 'true')
                page.evaluate("window.syntheticSpeech = []; voice.speakReply = async (text) => { if (typeof text === 'string') syntheticSpeech.push(text); voice.presentReply(text); };")
                for number, text in enumerate(('What is ready?', 'Read Dog Data', request,
                                                'While it runs what did the controller say?')):
                    page.locator('#phone-text').fill(text)
                    page.locator('#phone-input [type=submit]').click()
                    page.wait_for_function('(count) => jarvis.pending.size === 0 && speechRecords.length >= count', arg=number + 1)
                page.evaluate('jarvis.refresh()')
                page.wait_for_function("jarvis.summary.subject === 'Controller'")
                self.assertEqual(len(requests), 4)
                self.assertEqual(contexts[2]['subject'], self.targets['Dog Data'])
                self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])
                self.assertIn(body.encode(), self.received('Dog Data'))
                self.assertEqual(self.received('Dog Data').count(body.encode()), 1)
                originals = page.evaluate('speechRecords.filter(record => record.reply?.target === "Dog Data").map(record => record.reply.text)')
                self.assertIn('Five of six samples passed; one needs review.', originals)
                self.assertIn('Five of six samples passed; one needs review.', page.evaluate('syntheticSpeech'))
                self.assertTrue(all('all done' not in text for text in page.evaluate('syntheticSpeech')))
                self.assertEqual(scripts, [200])
                self.assertEqual(errors, [])
                output = Path(__file__).parent / '.run/jarvis-real-browser'
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                (output / 'browser.json').write_text(json.dumps({'passed': True, 'requests': len(requests),
                    'provider': 'synthetic', 'http_server': 'actual server.make_server',
                    'terminal': 'disposable actual session_actions paste/Enter with synthetic identity/history',
                    'checks': ['jarvis.js served', 'default Jarvis mode', 'ready/read/contextual send/read',
                               'subject independent of visible Notes', 'exact one delivery', 'original preserved',
                               'receipt-derived speech rejects raw done', 'no browser errors'],
                    'scope': 'Actual Chromium page and HTTP; voice speakReply replaced with capture, no microphone/physical phone/live model.'}, indent=2) + '\n')
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


    def test_actual_mobile_story_continues_while_observer_runs_and_completion_stays_quiet(self):
        follow_up = 'Ask it to explain those numbers with an example'
        message = 'explain those numbers with an example'
        correction = 'Actually, tell Controller to pause the queue.'
        self.allow(follow_up, body=message)
        self.allow(correction, 'Controller', 'pause the queue.')
        observer_started, observer_release = threading.Event(), threading.Event()
        contexts, decisions, requests, errors = [], [], [], []
        original_evaluate = self.app.evaluate
        def evaluate(payload):
            if 'decision' in payload['questions']:
                text = payload['state']['transcript']
                incomplete = text == 'Actually, tell Controller to'
                decisions.append({'text': text, 'complete': not incomplete})
                return {'answers': {'decision': {'choice': 'wait' if incomplete else 'complete'},
                                    'correction': {'choice': 'yes' if text.startswith('Actually,') else 'no'}}}
            return original_evaluate(payload)
        self.app.evaluate = evaluate
        def provider(context, specs, execute, **kwargs):
            contexts.append(context)
            text = context['current_turn']['user_text']
            if text == 'Read Dog Data':
                execute('read_reply', {'name': 'Dog Data'})
            elif text in (follow_up, correction):
                draft = execute('prepare_literal', {})
                execute('send_draft', {'draft_id': draft['draft_id']})
            elif text == 'What is Dog Data doing?':
                execute('observer_job', {'name': 'Dog Data'})
            elif text == 'While it runs what did the controller say?':
                execute('read_reply', {'name': 'Controller'})
            else:
                execute('job_status', {'job_id': next(iter(self.app.jarvis.snapshot()['jobs']))})
            return final('Untrusted model says everything was sent and finished.')
        self.provider = provider
        observer_original = 'Observer snapshot: Dog Data is waiting for review.'
        def observe(*args, **kwargs):
            observer_started.set()
            if not observer_release.wait(15):
                raise TimeoutError('Synthetic observer was not released')
            return {'text': observer_original, 'snapshot': {'observed_at': 'synthetic'}}
        self.app.focus(self.targets['Notes']['tab'])
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch('jarvis_tools.ask_session', side_effect=observe), sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.post_data_json)
                        if request.url.endswith('/api/jarvis/turn') else None)
                page.goto('http://127.0.0.1:' + str(server.server_port))
                page.wait_for_function('currentState !== null && jarvis.sessionId !== null')
                page.evaluate("""() => {
                    window.presented = []; window.spokenAttempts = [];
                    const speak = voice.speak.bind(voice);
                    voice.speak = (...args) => {spokenAttempts.push(args[0]); return speak(...args)};
                    const present = voice.presentReply.bind(voice);
                    voice.presentReply = text => {presented.push(text); return present(text)};
                    voice.silenceMs = 20; voice.recheckMs = 10000;
                }""")
                def typed(text, count):
                    page.locator('#phone-text').fill(text)
                    page.locator('#phone-input [type=submit]').click()
                    page.wait_for_function('(count) => jarvis.pending.size === 0 && speechRecords.length >= count', arg=count)
                typed('Read Dog Data', 1)
                typed(follow_up, 2)
                typed('What is Dog Data doing?', 3)
                self.assertTrue(observer_started.wait(1))
                job_id = next(iter(self.app.jarvis.snapshot()['jobs']))
                page.evaluate("voice.committed('While it runs what did the controller say?')")
                page.wait_for_function('jarvis.pending.size === 0 && speechRecords.length >= 4 && voice.draft === null')
                self.assertEqual(self.app.jarvis.snapshot()['jobs'][job_id]['status'], 'running')
                self.assertFalse(observer_release.is_set())
                page.wait_for_function("jarvis.snapshot.subject?.label === 'Controller'")
                page.evaluate("voice.committed('Actually, tell Controller to')")
                page.wait_for_function("voice.draft?.text === 'Actually, tell Controller to' && voice.draft.waiting")
                self.assertEqual(len(requests), 4)
                self.assertEqual(self.received('Controller'), b'')
                page.evaluate("voice.committed('pause the queue.')")
                page.wait_for_function('jarvis.pending.size === 0 && speechRecords.length >= 5 && voice.draft === null')
                self.assertEqual(requests[-1]['text'], correction)
                self.assertEqual(requests[-1]['source'], 'voice')
                self.assertIn('decision_id', requests[-1])
                self.assertFalse(requests[-1].get('explicit_send', False))
                self.assertEqual(self.received('Dog Data').count(message.encode()), 1)
                self.assertEqual(self.received('Controller').count(b'pause the queue.'), 1)
                self.assertEqual(self.app.jarvis.snapshot()['subject'], self.targets['Controller'])
                self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])
                before = page.evaluate('({presented:presented.length, speechAttempts:spokenAttempts.length, records:speechRecords.length, original:document.getElementById("reply-original").textContent})')
                observer_release.set()
                page.wait_for_function("""async (id) => {
                    await jarvis.refresh();
                    return jarvis.snapshot.jobs[id]?.status === 'completed';
                }""", arg=job_id)
                page.evaluate('refresh()')
                after = page.evaluate('({presented:presented.length, speechAttempts:spokenAttempts.length, records:speechRecords.length, original:document.getElementById("reply-original").textContent})')
                self.assertEqual(after, before)
                self.assertEqual(self.app.jarvis.snapshot()['subject'], self.targets['Controller'])
                self.assertEqual(self.app.jarvis.snapshot()['jobs'][job_id]['subject'], self.targets['Dog Data'])
                self.assertEqual(len(page.evaluate('currentState.job_notices')), 1)
                typed('What did that observation find?', 6)
                self.assertEqual(page.locator('#reply-original').text_content(), observer_original)
                self.assertTrue(next(iter(self.app.jarvis.snapshot()['notices'].values()))['consumed'])
                self.assertEqual(contexts[1]['subject'], self.targets['Dog Data'])
                self.assertTrue(any(item['text'] == 'Actually, tell Controller to' and not item['complete'] for item in decisions))
                self.assertTrue(any(item['text'] == correction and item['complete'] for item in decisions))
                originals = page.evaluate('speechRecords.filter(record => record.reply).map(record => record.reply.text)')
                self.assertIn('Five of six samples passed; one needs review.', originals)
                self.assertTrue(all('everything was sent and finished' not in text for text in page.evaluate('presented')))
                self.assertEqual(errors, [])
                output = Path(__file__).parent / '.run/jarvis-real-browser'
                output.mkdir(parents=True, exist_ok=True)
                page.locator('#speech-history > summary').click()
                self.assertIn(message, page.locator('#speech-records').inner_text())
                self.assertIn('pause the queue.', page.locator('#speech-records').inner_text())
                page.screenshot(path=str(output / 'final-story-mobile.png'), full_page=True)
                (output / 'final-story.json').write_text(json.dumps({'passed': True, 'turn_posts': requests,
                    'semantic_decisions': decisions, 'quiet_completion': before == after,
                    'job_subject': 'Dog Data', 'final_subject': 'Controller', 'visible_tab': 'Notes',
                    'terminal_inputs': 2, 'page_errors': errors,
                    'scope': 'Actual mobile-sized Chromium, VoiceControls semantic draft/continuation, JarvisClient, Python HTTP and disposable tmux; synthetic judgments/observer/provider, no microphone or audible playback. Actual aligned playback/stop uses the separate existing browser test.'}, indent=2) + '\n')
                browser.close()
        finally:
            observer_release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_actual_voice_readback_interrupt_records_only_proven_unicode_prefix(self):
        original = 'An exact original reply whose unheard suffix must remain available.'
        for module in ('codex_actions.thread_read', 'server.reply_history'):
            mocked = patch(module, return_value={'turns': [{'id': 'unicode-reply', 'status': 'completed', 'items': [
                {'type': 'agentMessage', 'phase': 'final_answer', 'text': original}]}]})
            mocked.start()
            self.addCleanup(mocked.stop)
        agent = self.app.jarvis_tools.agent(self.targets['Dog Data'])
        self.app.attention.observe(agent, {'turns': [{'id': 'unicode-reply', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': original}]}]})
        self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Dog Data'}) and final()
        prefix = 'Café 🐕. '
        rendition = prefix + 'Later words remain unread. '
        release = threading.Event()
        def render(text, mode, emit):
            emit(prefix)
            emit(rendition[len(prefix):])
            release.wait(15)
            raise RuntimeError('Synthetic delayed rendition failure')
        config = patch.object(self.app.voice, 'config', return_value={
            'configured': True, 'websocket_url': 'ws://controlled.invalid/speech'})
        config.start()
        self.addCleanup(config.stop)
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch('spoken_reply.render_spoken_reply', side_effect=render), sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.goto('http://127.0.0.1:' + str(server.server_port))
                page.wait_for_function('currentState !== null && jarvis.sessionId !== null')
                page.evaluate("""() => {
                  window.speechSockets = []; window.audioSources = [];
                  window.WebSocket = class {
                    static OPEN = 1;
                    constructor() { this.readyState = 1; this.sent = []; speechSockets.push(this); setTimeout(() => this.onopen?.(), 0); }
                    send(data) { this.sent.push(JSON.parse(data)); }
                    close() { this.readyState = 3; }
                    receive(message) { this.onmessage?.({data: JSON.stringify(message)}); }
                  };
                  const context = {
                    currentTime: 0, destination: {},
                    createBuffer(channels, samples, rate) { return {duration: samples / rate, getChannelData: () => new Float32Array(samples)}; },
                    createBufferSource() {
                      const source = {connect() {}, disconnect() {}, stop() {}, start() { this.lateEnd = this.onended; }};
                      audioSources.push(source); return source;
                    }
                  };
                  voice.capture = {ready: true, playback: context};
                  document.getElementById('speech-style').value = 'spoken';
                }""")
                page.evaluate('refresh()')
                page.locator('#phone-text').fill('Read Dog Data')
                page.locator('#phone-input [type=submit]').click()
                page.wait_for_function("""(text) => speechSockets.length &&
                    speechSockets[0].sent.map(m => m.text || '').join('') === text""", arg=rendition)
                page.evaluate("""() => {
                  const socket = speechSockets[0];
                  const phrases = socket.sent.filter(m => m.text).map(m => m.text);
                  const first = Array.from("Café 🐕. ").length;
                  const chars = Array.from(phrases.join(''));
                  socket.receive({alignment: {chars, starts: chars.map((_, i) => i < first ? i * 5 : 450 + (i - first) * 5), durations: chars.map(() => 5)}});
                  const audio = btoa(String.fromCharCode(0).repeat(19200));
                  socket.receive({audio}); socket.receive({audio});
                  window.savedPlayer = voice.playing;
                  audioSources[0].lateEnd();
                }""")
                self.assertEqual(page.evaluate('audioSources.length'), 2)
                playback_id = page.evaluate("""() =>
                  [...jarvis.owned.entries()].find(([, ledger]) => ledger.player === savedPlayer)[0]
                """)
                page.locator('#stop-readback').click()
                page.wait_for_function("""async id => {
                  const state = await (await fetch('/api/jarvis/state')).json();
                  const playback = state.playbacks[id];
                  return playback?.status === 'cancelled' && playback.heard_prefix?.chars === 8;
                }""", arg=playback_id)
                ledger = self.app.jarvis.snapshot()['playbacks'][playback_id]
                expected = {'chars': len(prefix), 'sha256': hashlib.sha256(prefix.encode()).hexdigest(), 'alignment': 'provider'}
                self.assertEqual(ledger['heard_prefix'], expected)
                self.assertEqual(ledger['source_ref']['original'], original)
                self.assertTrue(ledger['rendition_pending'])
                self.assertEqual(ledger['generated_text'], rendition)
                ready = self.app.attention.view(self.app.observe()['tabs'])
                self.assertTrue(any(item['text'] == original for item in ready))
                page.evaluate("""() => {
                  audioSources[1].lateEnd();
                  speechSockets[0].receive({audio: btoa(String.fromCharCode(0).repeat(19200)), isFinal: true});
                }""")
                release.set()
                page.wait_for_timeout(150)
                after = self.app.jarvis.snapshot()['playbacks'][ledger['playback_id']]
                self.assertEqual(after['heard_prefix'], expected)
                self.assertEqual(after['status'], 'cancelled')
                self.assertEqual(page.evaluate('audioSources.length'), 2)
                self.assertTrue(page.evaluate('voice.playing === null'))
                self.assertTrue(any(item['text'] == original for item in self.app.attention.view(self.app.observe()['tabs'])))
                output = Path(__file__).parent / '.run/jarvis-real-browser'
                output.mkdir(parents=True, exist_ok=True)
                (output / 'prefix.json').write_text(json.dumps({'passed': True, 'prefix': prefix, 'heard_prefix': expected,
                    'queued_audio_sources': 2, 'ended_before_stop': 1, 'original_unread': True,
                    'scope': 'Actual Chromium VoiceControls/ReadbackPlayer/JarvisClient -> actual Python HTTP ledger; controlled WebSocket alignment/audio and WebAudio ends, synthetic rendition provider; no microphone/physical phone.'}, indent=2) + '\n')
                browser.close()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_incidental_original_is_visible_after_send_or_preparation_and_reload_without_playback(self):
        original = 'Exact  original\nCafé 🐕. <b>not markup</b>  '
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        outcomes = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for send in (False, True):
                    with self.subTest(send=send), patch('jarvis_tools.read_reply', return_value=original):
                        request = 'Tell Dog Data: preserved words ' + str(send)
                        body = 'preserved words ' + str(send)
                        self.allow(request, body=body)
                        def provider(context, specs, execute, **kwargs):
                            execute('read_reply', {'name': 'Dog Data'})
                            draft = execute('prepare_literal', {})
                            if send:
                                execute('send_draft', {'draft_id': draft['draft_id']})
                            return final('Untrusted narration: all done.')
                        self.provider = provider
                        self.app.focus(self.targets['Notes']['tab'])
                        page = browser.new_page(viewport={'width': 390, 'height': 844})
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        def spy_voice(route):
                            response = route.fetch()
                            route.fulfill(response=response, body=response.text() + '''
                              const actualSpeakReply = VoiceControls.prototype.speakReply;
                              VoiceControls.prototype.speakReply = function(...args) {
                                (window.observedSpeechCalls ||= []).push(args[0]);
                                return actualSpeakReply.apply(this, args);
                              };
                            ''')
                        page.route('**/voice.js', spy_voice)
                        page.goto('http://127.0.0.1:' + str(server.server_port))
                        page.wait_for_function('currentState !== null && jarvis.sessionId !== null')
                        capture = """() => {
                          window.syntheticSpeech = [];
                          voice.speakReply = async (text, generation, spokenReady, onHeard, track) => {
                            syntheticSpeech.push(text);
                            voice.presentReply(text);
                            const player = {closed: false, stop() { this.closed = true; },
                              track(callback) { callback({status: 'playing'}); }};
                            track?.(player, {style: 'original', generation});
                          };
                        }"""
                        page.evaluate(capture)
                        page.locator('#phone-text').fill(request)
                        page.locator('#phone-input [type=submit]').click()
                        page.wait_for_function('(text) => jarvis.pending.size === 0 && speechRecords.some(item => item.text === text && item.availableReply)', arg=request)
                        record = page.evaluate('(text) => speechRecords.find(item => item.text === text)', request)
                        turn_id = record['reply']['turn_id']
                        canonical = self.app.jarvis.snapshot()['turns'][turn_id]['result']['text']
                        self.assertNotEqual(canonical, original)
                        self.assertEqual(record['reply']['text'], canonical)
                        self.assertEqual(record['availableReply']['text'], original)
                        self.assertEqual(page.evaluate('syntheticSpeech'), [canonical])
                        row = page.locator('#speech-records > p').filter(has_text=request).first
                        self.assertIn('Dog Data · preserved original reply\n' + original, row.locator('pre').all_text_contents())
                        self.assertEqual(row.locator('button', has_text='Read this reply').count(), 1)
                        self.assertEqual(row.locator('b').count(), 0)
                        page.wait_for_function('(id) => Object.values(jarvis.snapshot.playbacks).some(item => item.turn_id === id)', arg=turn_id)
                        playbacks_before = self.app.jarvis.snapshot()['playbacks']
                        own = [item for item in playbacks_before.values() if item['turn_id'] == turn_id]
                        self.assertTrue(own)
                        self.assertTrue(all(item['source_ref']['original'] == canonical for item in own))
                        self.assertTrue(all(item['reply_ref'] is None for item in own))
                        count_before_reload = len(playbacks_before)
                        page.reload()
                        page.wait_for_function('(id) => jarvis.snapshot?.turns[id] && speechRecords.some(item => item.reply?.turn_id === id && item.availableReply)', arg=turn_id)
                        reloaded = page.evaluate('(id) => speechRecords.find(item => item.reply?.turn_id === id)', turn_id)
                        self.assertEqual(reloaded['availableReply']['text'], original)
                        self.assertEqual(reloaded['reply']['text'], canonical)
                        page.wait_for_timeout(100)
                        self.assertEqual(len(self.app.jarvis.snapshot()['playbacks']), count_before_reload)
                        self.assertEqual(page.evaluate('window.observedSpeechCalls || []'), [])
                        page.evaluate(capture)
                        page.locator('#speech-history').evaluate('(element) => element.open = true')
                        row = page.locator('#speech-records > p').filter(has_text=request).first
                        row.locator('button', has_text='Read this reply').click()
                        page.wait_for_function('syntheticSpeech.length === 1')
                        self.assertEqual(page.evaluate('syntheticSpeech'), [canonical])
                        page.wait_for_function('(count) => Object.keys(jarvis.snapshot.playbacks).length > count', arg=count_before_reload)
                        latest = list(self.app.jarvis.snapshot()['playbacks'].values())[-1]
                        self.assertEqual(latest['source_ref']['original'], canonical)
                        self.assertNotEqual(latest['source_ref']['original'], original)
                        self.assertEqual(self.app.observe()['selected'], self.targets['Notes']['tab'])
                        actions = [item for item in self.app.jarvis.snapshot()['actions'].values() if item['turn_id'] == turn_id]
                        self.assertEqual(len(actions), int(send))
                        if send:
                            self.assertEqual(actions[0]['status'], 'submitted')
                            self.assertEqual(self.received('Dog Data').count(body.encode()), 1)
                        outcomes.append({'send': send, 'canonical': canonical, 'original': original,
                                         'reload_created_playback': False, 'manual_playback_source': 'canonical'})
                        page.close()
                browser.close()
            self.assertEqual(errors, [])
            output = Path(__file__).parent / '.run/jarvis-real-browser'
            output.mkdir(parents=True, exist_ok=True)
            (output / 'available-readback.json').write_text(json.dumps({'passed': True, 'cases': outcomes,
                'scope': 'Actual Chromium/HTTP/server state, synthetic provider, disposable tmux; speech captured with controlled playback player, no physical device or provider.'}, indent=2) + '\n')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

if __name__ == '__main__':
    unittest.main()
