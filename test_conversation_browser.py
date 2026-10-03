import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time
from playwright.sync_api import sync_playwright

from jarvis_controller import JarvisController

ROOT = Path(__file__).parent


def fixture_state():
    state = {'selected': '@a', 'selected_pane': '%a', 'api_configured': True,
             'groups': [{'id': 'workspace', 'name': 'Workspace'}],
             'voice': {'configured': False}, 'latest': None, 'pending_close': None,
             'pending_clarification': None, 'request_results': [], 'reply_notices': [], 'tabs': []}
    for letter in ('a', 'b'):
        state['tabs'].append({'id': '@' + letter, 'name': letter.upper(), 'index': len(state['tabs']),
            'group': 'Workspace', 'group_id': 'workspace', 'state': 'ready', 'agent': True,
            'panes': [{'id': '%' + letter, 'number': 1, 'active': True, 'identity': letter + ':original',
                       'command': 'codex', 'agent': True, 'codex': True, 'thread_id': letter,
                       'left': 0, 'top': 0, 'width': 120, 'height': 24,
                       'terminal_ansi': '\x1b[32m' + ('|' + '1234567890' * 11 + '       |\n') * 60 + '\x1b[0m', 'handoff': {}}]})
    return state


STATIC_FILES = ('index.html', 'voice.js', 'readback.js', 'jarvis.js', 'terminal.js')
SUBJECT_B = {'tab': '@b', 'pane': '%b', 'identity': 'b:original', 'label': 'B · pane 1'}
SPY_SPEECH = """() => {
  window.spoken = [];
  voice.speakReply = (text, generation) => { if (generation === voice.generation) spoken.push(text); };
  voice.speak = async (text, generation) => { if (generation === voice.generation) spoken.push(text); };
}"""


def submit_composer(page, text):
    page.locator('#phone-text').fill(text)
    page.locator('#phone-input [type=submit]').click()


def run():
    state = fixture_state()
    jarvis_state = {'session_id': 's1', 'revision': 1, 'subject': None, 'turns': {}, 'requests': {}, 'drafts': {},
                    'actions': {}, 'jobs': {}, 'playbacks': {}, 'clarifications': {}}
    jarvis_posts, jarvis_cancels = [], []
    requests, raw, errors, heard = [], [], [], []
    recommendation = {'id': 'attention-b', 'tab': '@b', 'pane': '%b',
                      'identity': 'b:original', 'label': 'B · pane 1'}
    def route(r):
        path = r.request.url.split('fixture.test')[-1]
        body = r.request.post_data_json if r.request.method == 'POST' else None
        if path == '/api/state':
            return r.fulfill(json=state)
        if path == '/api/jarvis/state':
            return r.fulfill(json=jarvis_state)
        if path == '/api/jarvis/turn':
            jarvis_posts.append(body)
            return r.fulfill(json={'session_id': 's1', 'turn_id': 'unused', 'request_id': body['request_id'],
                                   'revision': 2, 'status': 'completed', 'text': 'Unexpected default.', 'actions': []})
        if path == '/api/jarvis/cancel':
            jarvis_cancels.append(body)
            return r.fulfill(json={})
        if path == '/api/request':
            requests.append(body)
            commands = {"What's next?": 'recommend_next', 'Read it': 'read_recommended',
                        'Go there': 'select_recommended', 'What else is ready?': 'list_ready'}
            if body['request'] in commands:
                action = commands[body['request']]
                event = {'timestamp': str(len(requests)), 'request_id': body['request_id'],
                         'outcome': 'ok', 'action': action, 'delivery': 'not_attempted',
                         'recommendation': recommendation,
                         'output': 'Two chats ready. Start with B; it needs your input.'}
                if action in ('read_recommended', 'select_recommended'):
                    assert body['attention_target'] == recommendation
                    state.update(selected='@b', selected_pane='%b')
                if action == 'read_recommended':
                    event.update(output='The complete B reply.', heard_reply=recommendation)
                return r.fulfill(json=event)
            target = '@b' if 'Tell B:' in body['request'] else body['capture_target']['tab']
            event = {'timestamp': str(len(requests)), 'request_id': body['request_id'],
                     'outcome': 'ok', 'action': 'send_message', 'delivery': 'submitted',
                     'target': target, 'target_label': target[1:].upper() + ' · pane 1',
                     'execution': {'text': body['request']}}
            state['request_results'].append(event)
            return r.fulfill(json=event)
        if path == '/api/select':
            state.update(selected=body['tab'], selected_pane=body['tab'].replace('@', '%'))
            return r.fulfill(json=state)
        if path == '/api/input':
            raw.append(body)
            return r.fulfill(json=state)
        if path == '/api/voice-event':
            return r.fulfill(json={'ok': True})
        if path == '/api/replies/heard':
            heard.append(body)
            return r.fulfill(json={'ok': True})
        file = ROOT / ('index.html' if path == '/' else path.lstrip('/'))
        if file.name in STATIC_FILES:
            return r.fulfill(body=file.read_text(), content_type='text/html' if path == '/' else 'text/javascript')
        raise AssertionError('Unexpected endpoint: ' + path)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('**/*', route)
        audio_text = []
        def speech_socket(socket):
            def receive(raw):
                text = json.loads(raw)['text']
                audio_text.append(text)
                if text:
                    socket.send(json.dumps({'audio': base64.b64encode(bytes(96000)).decode()}))
                else:
                    socket.send(json.dumps({'isFinal': True}))
            socket.on_message(receive)
        page.route_web_socket(lambda url: True, speech_socket)
        page.goto('https://fixture.test/')
        page.wait_for_function('currentState !== null')
        def terminal_fits():
            page.wait_for_function("""[...document.querySelectorAll('.terminal')].every(el =>
              el.clientWidth > 0 && el.scrollWidth <= el.clientWidth + 1)""")
        terminal_fits()
        assert not page.locator('#session-rail').is_visible()
        assert page.locator('textarea:visible').count() == 1
        assert page.locator('#panes').bounding_box()['height'] > 550
        full_width = page.locator('#panes').bounding_box()['width']
        page.locator('#tabs-toggle').click()
        rail = page.locator('#session-rail').bounding_box()
        workspace = page.locator('#workspace').bounding_box()
        tabs = page.locator('.tab').all()
        assert rail['x'] == 0 and rail['x'] + rail['width'] <= workspace['x']
        assert tabs[1].bounding_box()['y'] >= tabs[0].bounding_box()['y'] + tabs[0].bounding_box()['height']
        assert page.locator('#panes').is_visible()
        terminal_fits()
        page.locator('#tabs-toggle').click()
        assert page.locator('#panes').bounding_box()['width'] == full_width
        terminal_fits()
        assert page.locator('#phone-text').is_visible()
        assert page.locator('#speech-style').input_value() == 'spoken'
        assert page.evaluate('inputMode') == 'jarvis'
        page.locator('#more-controls').evaluate('(el) => el.open = true')
        page.locator('#input-mode').select_option('direct')
        page.locator('#more-controls').evaluate('(el) => el.open = false')
        assert page.evaluate('inputMode') == 'direct'
        page.locator('#phone-text').fill('Please inspect B without changing my recipient.')
        page.evaluate("voice.committed('Then report back.'); voice.holdDraft()")
        mixed = 'Please inspect B without changing my recipient. Then report back.'
        assert page.locator('#phone-text').input_value() == mixed
        page.locator('#phone-input [type=submit]').click()
        page.wait_for_function('!submitting && speechRecords.length === 1')
        assert requests[-1]['source'] == 'typed' and requests[-1]['capture_target']['tab'] == '@a'
        assert requests[-1]['request'] == mixed
        assert not raw
        page.evaluate("voice.committed('Tell B: exact words.'); voice.holdDraft()")
        assert page.locator('#phone-text').input_value() == 'Tell B: exact words.'
        page.locator('#phone-input [type=submit]').click()
        page.wait_for_function('!submitting && speechRecords.length === 2')
        assert requests[-1]['source'] == 'voice'
        assert page.evaluate('currentState.selected') == '@a'
        page.locator('#phone-text').fill('Keep this draft in A.')
        page.evaluate("selectTab('@b')")
        page.wait_for_function("currentState.selected === '@b'")
        assert page.locator('#phone-text').input_value() == ''
        assert not page.locator('#speech-retarget').count()
        page.locator('#phone-text').fill('Separate draft in B.')
        state['reply_notices'].append({'id': 'reply-b', 'request_id': requests[1]['request_id'],
                                     'thread_id': 'b', 'target': 'B · pane 1', 'text': 'Original B reply.'})
        page.wait_for_function("document.getElementById('speech-records').textContent.includes('Original B reply.')")
        assert page.locator('#phone-text').input_value() == 'Separate draft in B.'
        page.locator('#speech-history > summary').click()
        page.locator('#read-ready-reply').click()
        assert page.locator('#reply-original').inner_text() == 'Original B reply.'
        page.locator('#speech-history > summary').click()
        page.evaluate("selectTab('@a')")
        page.wait_for_function("currentState.selected === '@a'")
        assert page.locator('#phone-text').input_value() == 'Keep this draft in A.'
        state['tabs'][0]['panes'][0]['identity'] = 'a:replacement'
        page.wait_for_function("phoneTarget().identity === 'a:replacement'")
        assert page.locator('#turn-status').inner_text() == 'Session changed. Review this draft, then press Send.'
        page.evaluate('voice.finishDraft()')
        assert len(requests) == 2
        page.locator('#phone-input [type=submit]').click()
        page.wait_for_function('!submitting && speechRecords.length === 3')
        assert requests[-1]['capture_target']['identity'] == 'a:replacement'
        assert requests[-1]['request'] == 'Keep this draft in A.'
        page.locator('#more-controls').evaluate('(el) => el.open = true')
        page.locator('#phone-escape').click()
        assert raw[-1]['key'] == 'Escape'
        page.locator('#phone-enter').click()
        assert raw[-1]['key'] == 'Enter'
        assert not page.locator('#paste-terminal').count()
        assert page.locator('textarea').count() == 1
        page.locator('#terminal-toggle').click()
        assert not page.locator('#panes').is_visible() and page.locator('#phone-text').is_visible()
        page.locator('#terminal-toggle').click()
        page.locator('#more-controls').evaluate('(el) => el.open = false')
        for width in (320, 390, 700, 1280):
            page.set_viewport_size({'width': width, 'height': 844})
            terminal_fits()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        assert page.locator('.terminal').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') > 10
        first = state['tabs'][0]['panes'][0]
        first['width'] = 60
        state['tabs'][0]['panes'].append({**first, 'id': '%split', 'number': 2, 'active': False, 'left': 61})
        page.wait_for_function("document.querySelectorAll('.terminal').length === 2")
        terminal_fits()
        assert page.locator('.terminal').first.text_content().count('1234567890') == 660
        state['tabs'][0]['panes'].pop()
        first['width'] = 120
        page.wait_for_function("document.querySelectorAll('.terminal').length === 1")
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#phone-text').fill('')
        page.set_viewport_size({'width': 390, 'height': 430})
        assert page.locator('#phone-input').bounding_box()['y'] + page.locator('#phone-input').bounding_box()['height'] <= 430
        assert page.locator('#panes').bounding_box()['height'] > 150
        page.set_viewport_size({'width': 390, 'height': 844})
        assert not errors, errors
        output = ROOT / '.run/conversation-flow'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'mobile.png'), full_page=True)
        state['voice'] = {'configured': True, 'websocket_url': 'wss://fixture.test/voice'}
        page.evaluate("""() => {
          voice.config = {websocket_url: 'wss://fixture.test/voice'};
          voice.draft = null;
          voice.drafts.clear();
          voice.capture = {ready: true, playback: new AudioContext()};
          window.announcements = [];
          window.originalSpeak = voice.speak;
          voice.speak = async text => announcements.push(text);
          document.getElementById('speech-style').value = 'original';
        }""")
        state['reply_notices'].append({'id': 'quiet-arrival', 'target': 'A', 'text': 'Quiet reply.'})
        page.wait_for_function("announcedReplies.has('quiet-arrival')")
        assert page.evaluate('announcements') == []
        selected = page.evaluate('currentState.selected')
        page.evaluate('submitRequest("What\'s next?")')
        assert page.evaluate('currentState.selected') == selected
        assert 'needs your input' in page.evaluate('announcements.at(-1)')
        assert not heard
        page.evaluate("submitRequest('Read it')")
        page.wait_for_function('voice.playing?.started')
        assert not heard
        page.locator('#stop-readback').click()
        assert not heard
        page.evaluate("submitRequest('Read it')")
        page.wait_for_function("document.getElementById('playback-status').textContent === 'Readback finished'")
        page.wait_for_timeout(100)
        assert heard == [recommendation]
        assert page.locator('#reply-original').text_content() == 'The complete B reply.'
        page.evaluate("submitRequest('Go there')")
        page.wait_for_function("currentState.selected === '@b'")
        page.evaluate("submitRequest('What else is ready?')")
        assert requests[-1]['request'] == 'What else is ready?'
        page.evaluate('voice.speak = originalSpeak; voice.capture.playback.close(); voice.capture = null')
        audio_text.clear()
        page.evaluate("""() => {
          voice.config = {websocket_url: 'wss://fixture.test/voice'};
          voice.draft = null;
          voice.capture = {ready: true, playback: new AudioContext()};
          window.audioEvents = [];
          voice.log = kind => audioEvents.push(kind);
          const originalFetch = fetch;
          window.fetch = (url, options) => {
            if (url !== '/api/request') return originalFetch(url, options);
            const encoder = new TextEncoder();
            const body = new ReadableStream({start(controller) {
              const emit = event => controller.enqueue(encoder.encode(JSON.stringify(event) + '\\n'));
              emit({type: 'text', text: 'Three checks passed. '});
              window.finishAnswer = () => {
                emit({type: 'text', text: 'Deployment is unverified.'});
                emit({type: 'result', result: {timestamp: 'stream', outcome: 'ok', action: 'ask_session',
                  output: 'Three checks passed. Deployment is unverified.'}});
                controller.close();
              };
            }});
            return Promise.resolve(new Response(body, {headers: {'content-type': 'application/x-ndjson'}}));
          };
          window.answerPending = submitRequest('Did it finish?');
        }""")
        page.wait_for_function("audioEvents.includes('playback_started')")
        assert page.evaluate('submitting')
        assert audio_text == ['Three checks passed. ']
        page.locator('#stop-readback').click()
        page.evaluate('finishAnswer(); answerPending')
        assert page.evaluate('voice.playing === null && !submitting')
        assert audio_text == ['Three checks passed. ']
        assert page.evaluate("audioEvents.filter(kind => kind === 'playback_started').length") == 1
        page.evaluate('voice.capture.playback.close(); voice.capture = null')
        assert not jarvis_posts and not jarvis_cancels
        page.locator('#more-controls').evaluate('(el) => el.open = true')
        page.locator('#input-mode').select_option('jarvis')
        page.locator('#more-controls').evaluate('(el) => el.open = false')
        jarvis_state.update(revision=2, subject=SUBJECT_B)
        page.wait_for_function("document.getElementById('jarvis-status').textContent.includes('Subject: B · pane 1')")
        direct_requests = len(requests)
        page.evaluate(SPY_SPEECH)
        page.evaluate("""() => {
          const originalFetch = window.fetch;
          window.gates = {}; window.turnPosts = []; window.dropNext = false; window.lostId = null;
          window.fetch = (url, options) => {
            if (url !== '/api/jarvis/turn') return originalFetch(url, options);
            const body = JSON.parse(options.body);
            turnPosts.push(body);
            if (dropNext) { dropNext = false; lostId = body.request_id; return Promise.reject(new TypeError('network lost')); }
            return new Promise(resolve => { gates[body.request_id] = receipt =>
              resolve(new Response(JSON.stringify(receipt), {headers: {'content-type': 'application/json'}})); });
          };
        }""")
        finished = lambda text, request, status='completed': {'session_id': 's1', 'turn_id': 't-' + request, 'request_id': request,
            'revision': 3, 'status': status, 'text': text, 'actions': []}
        page.locator('#phone-text').fill('Draft for Jarvis stays put.')
        page.evaluate("selectTab('@b')")
        page.wait_for_function("currentState.selected === '@b'")
        state['tabs'][1]['panes'][0]['identity'] = 'b:replaced'
        page.wait_for_function("phoneTarget().identity === 'b:replaced'")
        assert page.locator('#phone-text').input_value() == 'Draft for Jarvis stays put.'
        assert 'Session changed' not in page.locator('#turn-status').inner_text()
        assert 'Subject: B · pane 1' in page.locator('#jarvis-status').inner_text()
        page.locator('#phone-input [type=submit]').click()
        page.wait_for_function('turnPosts.length === 1')
        submit_composer(page, 'Second follow-up while the first waits.')
        page.wait_for_function('turnPosts.length === 2')
        first, second = page.evaluate('turnPosts')
        assert first['text'] == 'Draft for Jarvis stays put.' and second['text'] == 'Second follow-up while the first waits.'
        assert first['session_id'] == second['session_id'] == 's1' and first['request_id'] != second['request_id']
        assert all('capture_target' not in post for post in (first, second))
        assert len(requests) == direct_requests
        assert page.evaluate('jarvis.pending.size') == 2
        page.evaluate('(args) => gates[args[0]](args[1])', [second['request_id'], finished('Second answer.', second['request_id'])])
        page.wait_for_function('jarvis.pending.size === 1')
        page.evaluate('(args) => gates[args[0]](args[1])', [first['request_id'], finished('', first['request_id'], 'cancelled')])
        page.wait_for_function('jarvis.pending.size === 0')
        assert page.evaluate('spoken') == ['Second answer.']
        assert 'Superseded' in page.evaluate('speechRecords.find(r => r.id === %r).status' % first['request_id'])
        assert page.evaluate('speechRecords.find(r => r.id === %r).reply.text' % second['request_id']) == 'Second answer.'
        assert 'Subject: B · pane 1' in page.locator('#jarvis-status').inner_text()

        submit_composer(page, 'Answer arrives after Stop and listen.')
        page.wait_for_function('turnPosts.length === 3')
        stale = page.evaluate('turnPosts[2]')
        page.evaluate("voice.stopReadback('stop and listen')")
        page.evaluate('(args) => gates[args[0]](args[1])', [stale['request_id'], finished('Late but real answer.', stale['request_id'])])
        page.wait_for_function('jarvis.pending.size === 0')
        assert page.evaluate('spoken') == ['Second answer.']
        assert page.evaluate('speechRecords.find(r => r.id === %r).reply.text' % stale['request_id']) == 'Late but real answer.'

        page.evaluate('dropNext = true')
        submit_composer(page, 'The response to this one is lost.')
        page.wait_for_function('lostId !== null')
        lost = page.evaluate('lostId')
        assert page.evaluate('jarvis.pending.size') == 1
        jarvis_state.update(revision=6, requests={lost: 't-lost'}, turns={'t-lost': {
            'turn_id': 't-lost', 'request_id': lost, 'user_text': 'The response to this one is lost.', 'source': 'typed',
            'mode': 'jarvis', 'status': 'completed', 'result': {'text': 'Recovered from state.'}}})
        page.wait_for_function('jarvis.pending.size === 0')
        assert page.evaluate('turnPosts.filter(post => post.request_id === %r).length' % lost) == 1
        assert page.evaluate('spoken') == ['Second answer.', 'Recovered from state.']
        assert len(jarvis_cancels) == 0 and len(requests) == direct_requests
        assert page.evaluate('currentState.selected') == '@b'
        assert 'Subject: B · pane 1' in page.locator('#jarvis-status').inner_text()
        assert not errors, errors
        report = {'passed': True, 'requests': len(requests), 'terminal_inputs': len(raw),
                  'checks': ['typed/speech common path', 'exact words', 'cross-agent focus',
                             'separate drafts per pane', 'background original during draft',
                             'automatic identity replacement hold and reviewed manual send', 'explicit raw key', 'left mobile tabs',
                             'collapsible tabs', 'single typed and dictated composer',
                             'terminal height above 550px at 390x844', 'terminal toggle', '320–1280px overflow',
                             '120-column terminal fits after viewport, sidebar and split-pane resize',
                             'silent arrivals', 'recommend without switching', 'captured recommendation follow-up',
                             'completed audio acknowledges exact reply', 'interrupted reply stays unread',
                             'streamed observer audio before answer completion', 'stop prevents late streamed playback',
                             'default input reaches Jarvis only', 'Jarvis draft not held by pane selection or identity change',
                             'second request accepted while first waits', 'superseded and stopped answers stay visible without audio',
                             'lost response recovered from state without resend'],
                  'scope': 'Intercepted Chromium endpoints and a static Jarvis state; no model routing or physical phone proof'}
        (output / 'browser.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        browser.close()


class ControllerShim:
    """Test-local loopback HTTP adapter over the real JarvisController; it is not server.py."""

    def __init__(self):
        self.state = fixture_state()
        self.posts, self.provider_calls, self.drop = [], [], set()
        self.controller = JarvisController(self.provider, self.execute_tool)
        shim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, payload, status=200, content_type='application/json'):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == '/api/state':
                    return self.send(shim.state)
                if self.path == '/api/jarvis/state':
                    return self.send(shim.controller.snapshot())
                name = 'index.html' if self.path == '/' else self.path.lstrip('/')
                if name in STATIC_FILES:
                    return self.send((ROOT / name).read_bytes(), content_type='text/html' if name == 'index.html' else 'text/javascript')
                self.send({'error': 'unknown endpoint ' + self.path}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                if self.path == '/api/jarvis/turn':
                    shim.posts.append(body)
                    receipt = shim.controller.submit(body['text'], body['request_id'], body.get('capture_target'),
                                                     source=body['source'])
                    if body['text'] in shim.drop:
                        self.request.shutdown(socket.SHUT_RDWR)
                        return
                    return self.send(receipt)
                if self.path == '/api/jarvis/cancel':
                    return self.send(shim.controller.cancel(body.get('turn_id')) or {})
                if self.path == '/api/select':
                    shim.state.update(selected=body['tab'], selected_pane=body['tab'].replace('@', '%'))
                    return self.send(shim.state)
                if self.path == '/api/voice-event':
                    return self.send({'ok': True})
                self.send({'error': 'unknown endpoint ' + self.path}, 404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:%d/' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def provider(self, context, specs, execute, on_text=None, cancel=None):
        text = context['current_turn']['user_text']
        self.provider_calls.append(text)
        if text.startswith('Slow'):
            while not cancel.is_set():
                time.sleep(0.01)
            raise InterruptedError('superseded')
        if text.startswith('Focus'):
            execute('set_subject', {'target': SUBJECT_B})
            return {'text': 'Focused on B.'}
        return {'text': 'Echo: ' + text}

    def execute_tool(self, name, arguments, turn):
        assert name == 'set_subject'
        return {'ok': True, 'status': 'completed', 'subject': arguments['target']}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run_controller_shim():
    shim = ControllerShim()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(shim.url)
            page.wait_for_function('currentState !== null && jarvis.sessionId !== null')
            page.evaluate(SPY_SPEECH)
            assert page.evaluate('inputMode') == 'jarvis'

            submit_composer(page, 'Focus on B')
            page.wait_for_function("document.getElementById('jarvis-status').textContent.includes('Subject: B · pane 1')")
            page.evaluate("selectTab('@a')")
            page.wait_for_function("currentState.selected === '@a'")
            page.wait_for_timeout(700)
            assert shim.controller.snapshot()['subject']['tab'] == '@b'
            assert 'Subject: B · pane 1' in page.locator('#jarvis-status').inner_text()
            assert page.evaluate('spoken') == ['Focused on B.']

            submit_composer(page, 'Slow request that is superseded')
            page.wait_for_function('jarvis.pending.size === 1')
            submit_composer(page, 'Follow-up while the first is running')
            page.wait_for_function('jarvis.pending.size === 0')
            slow = next(t for t in shim.controller.snapshot()['turns'].values() if t['user_text'].startswith('Slow'))
            assert slow['status'] == 'cancelled'
            assert page.evaluate('spoken') == ['Focused on B.', 'Echo: Follow-up while the first is running']
            assert 'Superseded' in page.evaluate('speechRecords.find(r => r.text.startsWith("Slow")).status')

            shim.drop.add('This response is lost on the wire')
            submit_composer(page, 'This response is lost on the wire')
            page.wait_for_function('speechRecords.some(r => r.text === "This response is lost on the wire" && r.reply)')
            page.wait_for_function('jarvis.pending.size === 0')
            assert [post['text'] for post in shim.posts].count('This response is lost on the wire') == 1
            assert shim.provider_calls.count('This response is lost on the wire') == 1
            assert page.evaluate('spoken').count('Echo: This response is lost on the wire') == 1
            assert all(set(post) == {'session_id', 'request_id', 'text', 'source', 'stream'} for post in shim.posts)
            assert not errors, errors
            browser.close()
    finally:
        shim.close()
    report = {'passed': True, 'posts': len(shim.posts),
              'checks': ['real controller subject pinned across pane selection', 'real controller supersedes a running turn',
                         'lost HTTP response recovered from real state snapshot with one provider call'],
              'scope': 'Chromium against a test-local loopback adapter over the real JarvisController; not server.py'}
    print(json.dumps(report))

if __name__ == '__main__':
    run()
    run_controller_shim()
