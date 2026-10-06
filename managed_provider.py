from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from visual_workspace import KINDS, MAX_BODY, VisualError, fields, process_stamp


INSTRUCTIONS = '''Use the desk browser tools for browser tasks so the user sees your actual owned page. Use desk_publish_file or desk_create_document for every completed document/export; success returns its saved immutable creation/version receipt. Use desk_context at the start of each user request to read any selected output reference; preserve that creation_id when revising it. Never claim publication succeeded without a receipt. Login uses the Desk Sign in button; do not ask for passwords in chat or tools. Uninstrumented browsers are not visible in Desk. Use desk_app_start for development app previews.'''


def record_path(run_dir, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise VisualError('Invalid managed session', 403)
    return Path(run_dir) / 'managed-sessions' / (identifier + '.json')


def read_record(run_dir, identifier):
    path = record_path(run_dir, identifier)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return json.load(stream)


class ManagedProviders:
    def __init__(self, playground):
        self.app = playground
        self.run_dir = playground.log.parent
        self.lock = threading.RLock()
        self.path = self.run_dir / 'providers.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY,owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS references_inbox(request TEXT PRIMARY KEY,reference TEXT NOT NULL,recipient TEXT);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def owner(self, identifier, token):
        record = read_record(self.run_dir, identifier)
        if not isinstance(token, str) or not secrets.compare_digest(token, record['token']):
            raise VisualError('Managed session capability is invalid', 403)
        if process_stamp(record['pid']) != record['process']:
            raise VisualError('Managed provider process ended', 409)
        pane = self.app.validate_pane(record['tab'], record['pane'])
        if pane['process'] != record['process'] or pane['provider'] != record['provider']:
            raise VisualError('Managed provider pane was replaced', 409)
        target = {'tab': record['tab'], 'pane': record['pane'], 'identity': pane['identity']}
        from browser_http import browser_owner
        owner = browser_owner(self.app.visual, target)
        if record.get('resume') and owner['thread_id'] != record['resume']:
            raise VisualError('Managed resume selected a different conversation', 409)
        encoded = json.dumps(owner, sort_keys=True)
        with self.lock, self.db() as db:
            row = db.execute('SELECT owner FROM bindings WHERE id=?', (identifier,)).fetchone()
            if row and row[0] != encoded:
                raise VisualError('Managed conversation binding changed; relaunch explicitly', 409)
            if not row:
                db.execute('INSERT INTO bindings VALUES(?,?)', (identifier, encoded))
        return owner, record

    def stage_reference(self, request_id, value):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise VisualError('Output reference needs a request ID')
        reference = self.app.visual.reference(value)
        encoded = json.dumps(reference, sort_keys=True)
        with self.lock, self.db() as db:
            row = db.execute('SELECT reference FROM references_inbox WHERE request=?', (request_id,)).fetchone()
            if row and row[0] != encoded:
                raise VisualError('Request already belongs to another output reference', 409)
            db.execute('INSERT OR IGNORE INTO references_inbox VALUES(?,?,NULL)', (request_id, encoded))

    def bind_reference(self, request_id, agent):
        if not request_id:
            return
        with self.lock, self.db() as db:
            row = db.execute('SELECT reference,recipient FROM references_inbox WHERE request=?', (request_id,)).fetchone()
            if not row:
                return
            live = self.app.validate_pane(agent['window_id'], agent['pane_id'])
            if any(live.get(key) != agent.get(key) for key in ('provider', 'thread_id', 'process')):
                raise VisualError('Output reference recipient changed', 409)
            recipient = json.dumps({key: agent[key] for key in ('provider', 'thread_id', 'process')}, sort_keys=True)
            if row[1] and row[1] != recipient:
                raise VisualError('Reference delivery recipient changed', 409)
            db.execute('UPDATE references_inbox SET recipient=? WHERE request=?', (recipient, request_id))

    def context(self, owner):
        recipient = json.dumps({key: owner[key] for key in ('provider', 'thread_id', 'process')}, sort_keys=True)
        with self.db() as db:
            rows = db.execute('SELECT request,reference FROM references_inbox WHERE recipient=? ORDER BY rowid DESC LIMIT 8', (recipient,)).fetchall()
        references = []
        for request_id, encoded in rows:
            ref = json.loads(encoded)
            data, mime, _ = self.app.visual.content(ref['workspace_id'], ref['version_id'])
            references.append({'request_id': request_id, **ref, 'text': data[:32768].decode(errors='replace') if ref['kind'] in ('text', 'markdown', 'html') else None, 'truncated': len(data) > 32768})
        return {'references': references, 'supported': 'Managed Desk tools only; existing unrelated browser tools cannot be adopted automatically.'}

    def downloads(self, owner):
        result = self.app.browser.request('downloads', owner=owner)
        receipts = []
        for item in result.get('downloads', []):
            name = item['name']
            kind = next((kind for kind, extensions in KINDS.items() if Path(name).suffix.lower() in extensions), None)
            if kind is None:
                raise VisualError('Completed download format cannot be saved: ' + Path(name).suffix)
            receipt = self.app.visual.register({'target': owner['target'], 'title': name[:120], 'kind': kind,
                'mode': 'captured', 'entrypoint': name, 'files': [{'path': name, 'data': item['data']}],
                'receipt_key': item['receipt_key']})
            self.app.browser.request('ack_download', owner=owner, download_id=item['download_id'])
            receipts.append(receipt)
        return {'saved_downloads': receipts, 'download_errors': result.get('errors', [])}

    def call(self, identifier, token, tool, args):
        owner, record = self.owner(identifier, token)
        if not isinstance(args, dict):
            raise VisualError('Tool arguments must be an object')
        if tool == 'desk_context':
            fields(args, set())
            result = self.context(owner)
        elif tool == 'desk_browser_open':
            fields(args, {'url'})
            result = self.app.browser.request('open', owner=owner, profile_id=record['profile_id'], url=args['url'])
        elif tool == 'desk_browser_pages':
            fields(args, set())
            result = self.app.browser.request('pages', owner=owner)
        elif tool == 'desk_browser_action':
            fields(args, {'page_id', 'run_id', 'action', 'account_epoch'}, {'args'})
            result = self.app.browser.request('action', owner=owner, **args)
        elif tool == 'desk_publish_file':
            fields(args, {'title', 'kind', 'entrypoint', 'files', 'receipt_key'}, {'creation_id'})
            result = self.app.visual.register({'target': owner['target'], 'mode': 'captured', **args})
        elif tool == 'desk_app_start':
            fields(args, {'title', 'command'}, {'cwd', 'ready_path', 'build_identity'})
            result = self.app.apps.start(owner, record['workspace'], args)
        elif tool == 'desk_app_stop':
            fields(args, {'app_id'})
            result = self.app.apps.stop(owner, args['app_id'])
        elif tool == 'desk_app_state':
            fields(args, set())
            result = self.app.apps.state(owner=owner)
        else:
            raise VisualError('Unsupported Desk tool')
        current, _ = self.owner(identifier, token)
        if current != owner:
            raise VisualError('Conversation changed during tool execution', 409)
        if tool in ('desk_browser_open', 'desk_browser_action', 'desk_browser_pages'):
            result = {**result, **self.downloads(owner)}
        return result


def post_route(handler, providers):
    if handler.path != '/api/provider/tool':
        return False
    size = int(handler.headers.get('Content-Length', '0'))
    if handler.headers.get_content_type() != 'application/json' or not 0 < size <= MAX_BODY:
        raise VisualError('Send a bounded provider request')
    body = json.loads(handler.rfile.read(size))
    fields(body, {'session_id', 'tool', 'arguments'})
    token = handler.headers.get('Authorization', '').removeprefix('Bearer ')
    handler.send(200, providers.call(body['session_id'], token, body['tool'], body['arguments']))
    return True
