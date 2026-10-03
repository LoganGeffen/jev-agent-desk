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
                self.assertEqual(page.locator('#input-mode').input_value(), 'jarvis')
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
                page.locator('#stop-readback').click()
                page.wait_for_function("""async () => {
                  const state = await (await fetch('/api/jarvis/state')).json();
                  return Object.values(state.playbacks).some(p => p.status === 'cancelled' && p.heard_prefix?.chars === 8);
                }""")
                ledger = next(iter(self.app.jarvis.snapshot()['playbacks'].values()))
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

if __name__ == '__main__':
    unittest.main()
