import base64
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent


def run():
    state = {'selected': '@a', 'selected_pane': '%a', 'api_configured': True,
             'groups': [{'id': 'workspace', 'name': 'Workspace'}],
             'voice': {'configured': False}, 'latest': None, 'pending_close': None,
             'pending_clarification': None, 'request_results': [], 'reply_notices': [], 'tabs': []}
    for letter in ('a', 'b'):
        state['tabs'].append({'id': '@' + letter, 'name': letter.upper(), 'index': len(state['tabs']),
            'group': 'Workspace', 'group_id': 'workspace', 'state': 'ready', 'agent': True,
            'panes': [{'id': '%' + letter, 'number': 1, 'active': True, 'identity': letter + ':original',
                       'command': 'codex', 'agent': True, 'codex': True, 'thread_id': letter,
                       'left': 0, 'top': 0, 'width': 80, 'height': 24, 'terminal_ansi': '', 'handoff': {}}]})
    requests, raw, errors = [], [], []
    def route(r):
        path = r.request.url.split('fixture.test')[-1]
        body = r.request.post_data_json if r.request.method == 'POST' else None
        if path == '/api/state':
            return r.fulfill(json=state)
        if path == '/api/request':
            requests.append(body)
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
        file = ROOT / ('index.html' if path == '/' else path.lstrip('/'))
        if file.name in ('index.html', 'voice.js', 'readback.js', 'terminal.js'):
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
        page.locator('#tabs-toggle').click()
        assert page.locator('#panes').bounding_box()['width'] == full_width
        assert page.locator('#phone-text').is_visible()
        assert page.locator('#speech-style').input_value() == 'auto'
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
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
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
        assert not errors, errors
        report = {'passed': True, 'requests': len(requests), 'terminal_inputs': len(raw),
                  'checks': ['typed/speech common path', 'exact words', 'cross-agent focus',
                             'separate drafts per pane', 'background original during draft',
                             'automatic identity replacement hold and reviewed manual send', 'explicit raw key', 'left mobile tabs',
                             'collapsible tabs', 'single typed and dictated composer',
                             'terminal height above 550px at 390x844', 'terminal toggle', '320–1280px overflow',
                             'streamed observer audio before answer completion', 'stop prevents late streamed playback'],
                  'scope': 'Intercepted Chromium endpoints; no model routing or physical phone proof'}
        (output / 'browser.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        browser.close()


if __name__ == '__main__':
    run()
