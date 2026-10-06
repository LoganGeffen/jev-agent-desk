import json
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from browser_runtime import BrowserError, ManagedBrowser
from visual_workspace import VisualError, fields


STREAMS = threading.BoundedSemaphore(8)


def browser_owner(visual, target):
    pane = visual.target(target)
    return {'workspace_id': visual.workspace(target['tab']), 'target': dict(target),
            **{key: pane[key] for key in ('provider', 'thread_id', 'process', 'agent_process')}}


def create_browser(visual):
    browser = ManagedBrowser(visual.playground.log.parent)
    browser.validate_owner = lambda owner: browser_owner(visual, owner['target']) == owner
    return browser


def public_page(page):
    result = {key: value for key, value in page.items() if key != 'owner'}
    query = {**page['target'], 'page_id': page['page_id'], 'run_id': page['run_id']}
    result['stream_url'] = '/api/browser/stream?' + urlencode(query)
    return result


def browser_state(visual, browser, tab):
    workspace = visual.workspace(tab)
    pages = []
    for pane in visual.playground.panes(tab):
        if not pane.get('thread_id') or pane.get('provider') not in ('codex', 'claude'):
            continue
        target = {'tab': tab, 'pane': pane['id'], 'identity': pane['identity']}
        try:
            owner = browser_owner(visual, target)
            records = browser.request('view_pages', owner=owner)['pages']
            if browser_owner(visual, target) != owner:
                continue
            pages.extend(public_page(record) for record in records)
        except (VisualError, BrowserError):
            continue
    return {'workspace_id': workspace, 'pages': pages, **browser.request('profiles')}


def get_route(handler, visual, browser):
    parsed = urlsplit(handler.path)
    if not parsed.path.startswith('/api/browser/'):
        return False
    if not handler.local_request() or handler.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
        raise BrowserError('Cross-origin browser requests are not allowed', 403)
    query = parse_qs(parsed.query)
    if any(len(values) != 1 for values in query.values()):
        raise BrowserError('Duplicate browser query field', 400)
    args = {key: values[0] for key, values in query.items()}
    if parsed.path == '/api/browser/state':
        fields(args, {'tab'})
        handler.send(200, browser_state(visual, browser, args['tab']))
        return True
    if parsed.path != '/api/browser/stream':
        raise BrowserError('Unknown browser route', 404)
    fields(args, {'tab', 'pane', 'identity', 'page_id', 'run_id'}, {'client_id'})
    target = {key: args.pop(key) for key in ('tab', 'pane', 'identity')}
    args.pop('client_id', None)
    owner = browser_owner(visual, target)
    if not STREAMS.acquire(blocking=False):
        raise BrowserError('Browser observer limit reached', 429)
    sequence = 0
    try:
        first = browser.request('frame', owner=owner, **args)
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.end_headers()
        sequence = 0
        while True:
            if browser_owner(visual, target) != owner:
                raise BrowserError('Conversation identity changed; browser observation stopped')
            frame = first if sequence == 0 else browser.request('frame', owner=owner, **args)
            sequence += 1
            frame['sequence'] = sequence
            handler.wfile.write(('event: frame\ndata: ' + json.dumps(frame) + '\n\n').encode())
            handler.wfile.flush()
            time.sleep(.5)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except (BrowserError, VisualError, OSError) as error:
        if sequence:
            try:
                handler.wfile.write(('event: status\ndata: ' + json.dumps({'page_id': args['page_id'], 'run_id': args['run_id'], 'status': 'stale', 'error': str(error)}) + '\n\n').encode())
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            raise
    finally:
        STREAMS.release()
    return True


def post_route(handler, visual, browser):
    if not handler.path.startswith('/api/browser/'):
        return False
    op = handler.path.removeprefix('/api/browser/')
    schemas = {
        'open': ({'target'}, {'profile_id', 'url', 'account_label'}),
        'action': ({'target', 'page_id', 'run_id', 'action', 'account_epoch'}, {'args'}),
        'lease': ({'target', 'page_id', 'run_id', 'client_id'}, set()),
        'input': ({'target', 'page_id', 'run_id', 'client_id', 'lease_id', 'sequence', 'action'}, {'args'}),
        'done': ({'target', 'page_id', 'run_id', 'client_id', 'lease_id'}, set()),
        'disconnect': ({'target', 'page_id', 'run_id', 'client_id', 'lease_id'}, set()),
        'heartbeat': ({'target', 'page_id', 'run_id', 'client_id', 'lease_id'}, set()),
    }
    if op not in schemas:
        raise BrowserError('Unknown browser operation', 404)
    length = int(handler.headers.get('Content-Length', '0'))
    if handler.headers.get_content_type() != 'application/json' or not 0 < length <= 128 * 1024:
        raise BrowserError('Send a bounded JSON browser request', 400)
    body = json.loads(handler.rfile.read(length))
    fields(body, *schemas[op])
    target = body.pop('target')
    owner = browser_owner(visual, target)
    result = browser.request(op, owner=owner, **body)
    if browser_owner(visual, target) != owner:
        raise BrowserError('Conversation identity changed; browser result is unavailable')
    handler.send(200, public_page(result) if 'page_id' in result and 'target' in result else result)
    return True
