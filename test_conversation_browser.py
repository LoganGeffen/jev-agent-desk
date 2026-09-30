import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent


def run():
    state = {'selected': '@a', 'selected_pane': '%a', 'api_configured': True,
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
        if file.name in ('index.html', 'voice.js', 'terminal.js'):
            return r.fulfill(body=file.read_text(), content_type='text/html' if path == '/' else 'text/javascript')
        raise AssertionError('Unexpected endpoint: ' + path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('**/*', route)
        page.goto('https://fixture.test/')
        page.wait_for_function('currentState !== null')
        assert page.locator('#speech-style').input_value() == 'auto'
        page.locator('#phone-text').fill('Please inspect B without changing my recipient.')
        page.locator('#phone-input button').click()
        page.wait_for_function('!submitting && speechRecords.length === 1')
        assert requests[-1]['source'] == 'typed' and requests[-1]['capture_target']['tab'] == '@a'
        assert not raw
        page.evaluate("voice.committed('Tell B: exact words.'); voice.holdDraft()")
        assert page.locator('#phone-text').input_value() == 'Tell B: exact words.'
        page.locator('#phone-input button').click()
        page.wait_for_function('!submitting && speechRecords.length === 2')
        assert requests[-1]['source'] == 'voice'
        assert page.evaluate('currentState.selected') == '@a'
        page.locator('#phone-text').fill('Keep this draft in A.')
        page.evaluate("selectTab('@b')")
        page.wait_for_function("currentState.selected === '@b'")
        page.locator('#phone-input button').click()
        assert len(requests) == 2
        assert page.locator('#phone-text').input_value() == 'Keep this draft in A.'
        state['reply_notices'].append({'id': 'reply-b', 'request_id': requests[1]['request_id'],
                                     'thread_id': 'b', 'target': 'B · pane 1', 'text': 'Original B reply.'})
        page.wait_for_function("document.getElementById('speech-records').textContent.includes('Original B reply.')")
        assert page.locator('#phone-text').input_value() == 'Keep this draft in A.'
        page.locator('#read-ready-reply').click()
        assert page.locator('#reply-original').inner_text() == 'Original B reply.'
        page.evaluate("selectTab('@a')")
        page.wait_for_function("currentState.selected === '@a'")
        state['tabs'][0]['panes'][0]['identity'] = 'a:replacement'
        page.wait_for_function("phoneTarget().identity === 'a:replacement'")
        page.locator('#phone-input button').click()
        assert len(requests) == 2
        assert page.locator('#phone-text').input_value() == 'Keep this draft in A.'
        page.locator('#more-controls').evaluate('(el) => el.open = true')
        page.locator('#phone-escape').click()
        assert raw[-1]['key'] == 'Escape' and len(requests) == 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        output = ROOT / '.run/conversation-flow'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'mobile.png'), full_page=True)
        report = {'passed': True, 'requests': len(requests), 'raw_keys': len(raw),
                  'checks': ['typed/speech common path', 'exact words', 'cross-agent focus',
                             'draft navigation hold', 'background original during draft',
                             'identity replacement hold', 'explicit raw key', 'mobile overflow'],
                  'scope': 'Intercepted Chromium endpoints; no model routing or physical phone proof'}
        (output / 'browser.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        browser.close()


if __name__ == '__main__':
    run()
