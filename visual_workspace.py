import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import threading
import time
import uuid
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
import zipfile

from codex_actions import tmux
from websockets.sync.client import connect


MAX_FILE = 8 * 1024 * 1024
MAX_VERSION = 16 * 1024 * 1024
MAX_FILES = 128
MAX_BODY = 24 * 1024 * 1024
KINDS = {'html': {'.html', '.htm'}, 'markdown': {'.md', '.markdown'},
         'text': {'.txt', '.csv', '.log'}, 'pdf': {'.pdf'},
         'image': {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'}}
EXTENSIONS = set().union(*KINDS.values()) | {'.js', '.mjs', '.css', '.svg', '.woff', '.woff2', '.ttf', '.json'}


class VisualError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise VisualError('Missing or unknown visual workspace fields')


def visible(value, label):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 or any(ord(c) < 32 for c in value):
        raise VisualError(f'A visible {label} of at most 120 characters is required')
    return value.strip()


def safe_path(value):
    if not isinstance(value, str) or len(value) > 240 or '\\' in value or '%' in value or any(ord(c) < 32 for c in value):
        raise VisualError('Invalid bundle path')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or str(path) != value or any(part.startswith('.') for part in path.parts):
        raise VisualError('Relative non-hidden bundle paths are required')
    for part in path.parts:
        if re.search(r'(^|[-_.])(credentials?|secrets?|tokens?|private[-_]?keys?|id_rsa|id_ed25519)([-_.]|$)', part, re.I):
            raise VisualError('Credential and private runtime files cannot be captured')
    if path.suffix.lower() not in EXTENSIONS:
        raise VisualError('Unsupported bundle file type')
    return value


def process_stamp(pid):
    stat = Path(f'/proc/{pid}/stat').read_text()
    return f'{pid}:' + stat.rsplit(')', 1)[1].split()[19]



def agent_process(pane):
    pending = [str(pane['process']).split(':', 1)[0]]
    matches = []
    try:
        while pending:
            pid = pending.pop(0)
            proc = Path('/proc') / pid
            if (proc / 'comm').read_text().strip() == pane['provider']:
                matches.append(process_stamp(pid))
            else:
                pending.extend((proc / 'task' / pid / 'children').read_text().split())
    except OSError:
        raise VisualError('Agent process changed; refresh before capturing', 409) from None
    if len(matches) != 1:
        raise VisualError('Agent process ownership is unresolved', 409)
    return matches[0]


def endpoint_identity(pid, port, session):
    if type(pid) is not int or pid <= 0 or type(port) is not int or not 1 <= port <= 65535:
        raise VisualError('A live browser PID and loopback stream port are required')
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session) or session == 'default':
        raise VisualError('Use an explicitly named agent-browser session')
    try:
        proc = Path(f'/proc/{pid}')
        started = process_stamp(pid)
        executable = Path(os.readlink(proc / 'exe')).name
        env = dict(item.split(b'=', 1) for item in (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
        if not executable.startswith('agent-browser') or env.get(b'AGENT_BROWSER_DAEMON') != b'1' or env.get(b'AGENT_BROWSER_SESSION') != session.encode():
            raise VisualError('The listener is not the named agent-browser daemon', 409)
        inodes = set()
        for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            cells = row.split()
            if cells[1] == f'0100007F:{port:04X}' and cells[3] == '0A':
                inodes.add(cells[9])
        sockets = set()
        for fd in (proc / 'fd').iterdir():
            try:
                link = os.readlink(fd)
                if link.startswith('socket:['):
                    sockets.add(link[8:-1])
            except FileNotFoundError:
                continue
        matches = sockets & inodes
        if len(matches) != 1 or process_stamp(pid) != started:
            raise VisualError('Browser stream listener ownership changed', 409)
        return {'process': started, 'inode': next(iter(matches))}
    except OSError:
        raise VisualError('Browser process or stream listener is unavailable', 409) from None


class VisualWorkspace:
    def __init__(self, playground):
        self.playground = playground
        self.path = playground.log.parent / 'visual.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.live = {}
        self.streaming = set()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS creations(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, creation TEXT NOT NULL, number INTEGER NOT NULL, created_at TEXT NOT NULL, entrypoint TEXT NOT NULL, byte_size INTEGER NOT NULL, source TEXT NOT NULL, token TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS files(version TEXT NOT NULL, path TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(version,path));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def owner(self, tab):
        if not isinstance(tab, str) or not re.fullmatch(r'@\d+', tab):
            raise VisualError('A current Desk tab is required')
        app = self.playground
        windows = tmux(app.socket, 'list-windows', '-t', f'={app.source_session}', '-F', '#{window_id}').splitlines()
        if tab not in windows:
            raise VisualError('That tab is no longer available', 409)
        pid = int(tmux(app.socket, 'display-message', '-p', '#{pid}').strip())
        socket = Path(app.socket).stat()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return ':'.join((boot, process_stamp(pid), str(socket.st_dev), str(socket.st_ino), tab))

    def workspace(self, tab):
        owner = self.owner(tab)
        with self.db() as db:
            row = db.execute('SELECT id FROM workspaces WHERE owner=?', (owner,)).fetchone()
            if row:
                return row['id']
            identifier = str(uuid.uuid4())
            db.execute('INSERT INTO workspaces VALUES(?,?)', (identifier, owner))
            return identifier

    def target(self, value):
        fields(value, {'tab', 'pane', 'identity'})
        if not all(isinstance(v, str) and v for v in value.values()):
            raise VisualError('An exact pane/conversation identity is required')
        try:
            live = self.playground.validate_pane(value['tab'], value['pane'], value['identity'])
        except ValueError:
            raise VisualError('That pane or conversation changed; refresh before capturing', 409) from None
        if not live.get('thread_id') or live.get('provider') not in ('claude', 'codex') or not live.get('process'):
            raise VisualError('Identify the agent conversation before capturing or attaching', 409)
        return {**live, 'agent_process': agent_process(live)}

    def register(self, body):
        fields(body, {'target', 'title', 'kind', 'mode', 'entrypoint', 'files'}, {'creation_id'})
        pane = self.target(body['target'])
        workspace = self.workspace(body['target']['tab'])
        title = visible(body['title'], 'creation title')
        kind = body['kind']
        if kind not in KINDS or body['mode'] not in ('captured', 'imported'):
            raise VisualError('Unsupported creation kind or provenance mode')
        entrypoint = safe_path(body['entrypoint'])
        if PurePosixPath(entrypoint).suffix.lower() not in KINDS[kind]:
            raise VisualError('Entrypoint type does not match creation kind')
        if not isinstance(body['files'], list) or not 1 <= len(body['files']) <= MAX_FILES:
            raise VisualError(f'A version needs 1 to {MAX_FILES} files')
        files = {}
        size = 0
        for item in body['files']:
            fields(item, {'path', 'data'})
            name = safe_path(item['path'])
            if name in files or not isinstance(item['data'], str) or len(item['data']) > (MAX_FILE * 4 // 3 + 4):
                raise VisualError('Duplicate file or file exceeds capture limit')
            try:
                data = base64.b64decode(item['data'], validate=True)
            except ValueError:
                raise VisualError('File data must be base64') from None
            size += len(data)
            if len(data) > MAX_FILE or size > MAX_VERSION:
                raise VisualError('Capture exceeds 8 MiB per file or 16 MiB per version')
            files[name] = data
        if entrypoint not in files or (kind != 'html' and len(files) != 1):
            raise VisualError('Missing entrypoint or unsupported non-HTML bundle')
        creation = body.get('creation_id') or str(uuid.uuid4())
        if not isinstance(creation, str):
            raise VisualError('Invalid creation ID')
        version = str(uuid.uuid4())
        source = {key: pane.get(key) for key in ('provider', 'thread_id', 'process', 'agent_process')}
        source.update(mode=body['mode'], pane=body['target']['pane'], identity=body['target']['identity'])
        with self.db() as db:
            row = db.execute('SELECT * FROM creations WHERE id=?', (creation,)).fetchone()
            if body.get('creation_id') and (row is None or row['workspace'] != workspace or row['kind'] != kind):
                raise VisualError('Creation does not belong to this workspace and kind', 409)
            if self.target(body['target'])['agent_process'] != pane['agent_process']:
                raise VisualError('Agent process changed before capture', 409)
            if self.workspace(body['target']['tab']) != workspace:
                raise VisualError('Tab incarnation changed before capture', 409)
            if row is None:
                db.execute('INSERT INTO creations VALUES(?,?,?,?)', (creation, workspace, title, kind))
            number = db.execute('SELECT count(*) FROM versions WHERE creation=?', (creation,)).fetchone()[0] + 1
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)', (version, creation, number,
                datetime.now(timezone.utc).isoformat(), entrypoint, size, json.dumps(source), secrets.token_urlsafe(32)))
            db.executemany('INSERT INTO files VALUES(?,?,?)', [(version, name, data) for name, data in files.items()])
        return {'workspace_id': workspace, 'creation_id': creation, 'version_id': version}

    def list(self, tab):
        workspace = self.workspace(tab)
        creations = []
        with self.db() as db:
            for row in db.execute('SELECT * FROM creations WHERE workspace=? ORDER BY rowid DESC', (workspace,)):
                creation = {key: row[key] for key in ('id', 'title', 'kind')}
                creation['versions'] = []
                for version in db.execute('SELECT * FROM versions WHERE creation=? ORDER BY number DESC', (row['id'],)):
                    item = {key: version[key] for key in ('id', 'number', 'created_at', 'entrypoint', 'byte_size')}
                    query = urlencode({'workspace': workspace, 'version': version['id']})
                    item.update(source=json.loads(version['source']), content_url='/api/visual/content?' + query,
                                download_url='/api/visual/download?' + query,
                                preview_url='/visual-preview/' + version['token'] + '/' + quote(version['entrypoint']) if creation['kind'] == 'html' else None)
                    creation['versions'].append(item)
                creations.append(creation)
        live = self.live.get(workspace)
        if live:
            try:
                self.validate_live(workspace, live)
            except VisualError as error:
                live.update(status='stale', error=str(error))
        return {'workspace_id': workspace, 'tab': tab, 'creations': creations, 'live': self.live_view(workspace, live) if live else None}

    def content(self, workspace, version, download=False):
        with self.db() as db:
            row = db.execute('SELECT v.*,c.kind FROM versions v JOIN creations c ON v.creation=c.id WHERE v.id=? AND c.workspace=?', (version, workspace)).fetchone()
            if row is None:
                raise VisualError('Saved version not found in this workspace', 404)
            files = db.execute('SELECT path,data FROM files WHERE version=? ORDER BY path', (version,)).fetchall()
        entry = next(file for file in files if file['path'] == row['entrypoint'])
        if download and len(files) > 1:
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
                for file in files:
                    archive.writestr(file['path'], file['data'])
            return data.getvalue(), 'application/zip', 'creation-' + version + '.zip'
        mime = mimetypes.guess_type(entry['path'])[0] or 'application/octet-stream'
        if row['kind'] in ('html', 'markdown', 'text'):
            mime = 'text/plain; charset=utf-8'
        return entry['data'], mime, PurePosixPath(entry['path']).name

    def preview(self, token, name):
        safe_path(name)
        with self.db() as db:
            row = db.execute('SELECT f.data FROM files f JOIN versions v ON f.version=v.id JOIN creations c ON c.id=v.creation WHERE v.token=? AND f.path=? AND c.kind=?', (token, name, 'html')).fetchone()
        if row is None:
            raise VisualError('Preview file not found', 404)
        return row['data'], mimetypes.guess_type(name)[0] or 'application/octet-stream'

    def live_view(self, workspace, live):
        result = {key: live[key] for key in ('id', 'generation', 'status', 'name', 'session')}
        result.update(pane=live['target']['pane'], identity=live['target']['identity'], error=live.get('error'),
                      stream_url='/api/visual/live?' + urlencode({'workspace': workspace, 'attachment': live['id'], 'generation': live['generation']}))
        return result

    def attach(self, body):
        fields(body, {'target', 'name', 'session', 'port', 'pid'})
        pane = self.target(body['target'])
        workspace = self.workspace(body['target']['tab'])
        endpoint = endpoint_identity(body['pid'], body['port'], body['session'])
        live = {**body, 'name': visible(body['name'], 'browser name'), 'endpoint': endpoint, 'agent_process': pane['agent_process'],
                'id': str(uuid.uuid4()), 'generation': str(uuid.uuid4()), 'status': 'disconnected'}
        with self.lock:
            if self.target(body['target'])['agent_process'] != pane['agent_process']:
                raise VisualError('Agent process changed before attachment', 409)
            if self.workspace(body['target']['tab']) != workspace:
                raise VisualError('Tab changed before attachment', 409)
            self.live[workspace] = live
        return {'workspace_id': workspace, 'live': self.live_view(workspace, live)}

    def detach(self, body):
        fields(body, {'tab', 'attachment_id'})
        workspace = self.workspace(body['tab'])
        with self.lock:
            if self.live.get(workspace, {}).get('id') != body['attachment_id']:
                raise VisualError('Browser attachment changed', 409)
            self.live.pop(workspace)
        return {'ok': True}

    def validate_live(self, workspace, live):
        if live.get('status') == 'stale' or self.live.get(workspace) is not live:
            raise VisualError('Browser attachment changed; attach explicitly again', 409)
        if self.target(live['target'])['agent_process'] != live['agent_process']:
            raise VisualError('Agent process changed; attach explicitly again', 409)
        if self.workspace(live['target']['tab']) != workspace or endpoint_identity(live['pid'], live['port'], live['session']) != live['endpoint']:
            raise VisualError('Browser or tab incarnation changed; attach explicitly again', 409)

    def stream(self, handler, workspace, attachment, generation):
        live = self.live.get(workspace)
        if live is None or live['id'] != attachment or live['generation'] != generation:
            raise VisualError('Browser attachment changed', 409)
        self.validate_live(workspace, live)
        with self.lock:
            if attachment in self.streaming:
                raise VisualError('This browser already has an active viewer', 409)
            self.streaming.add(attachment)
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        def emit(event, body):
            handler.wfile.write(('event: ' + event + '\ndata: ' + json.dumps({'generation': generation, **body}) + '\n\n').encode())
            handler.wfile.flush()
        try:
            live['status'] = 'connecting'
            emit('status', {'status': 'connecting'})
            with connect(f"ws://127.0.0.1:{live['port']}", origin=f"http://127.0.0.1:{live['port']}", proxy=None,
                         open_timeout=3, close_timeout=1, max_size=MAX_FILE, max_queue=2) as upstream:
                self.validate_live(workspace, live)
                sequence = 0
                next_frame = 0
                last_check = 0
                while True:
                    if time.monotonic() - last_check >= 1:
                        self.validate_live(workspace, live)
                        last_check = time.monotonic()
                    try:
                        message = upstream.recv(timeout=1)
                    except TimeoutError:
                        emit('status', {'status': live['status']})
                        continue
                    packet = json.loads(message)
                    if not isinstance(packet, dict):
                        continue
                    if packet.get('type') == 'status' and packet.get('connected') is False:
                        live['status'] = 'disconnected'
                        emit('status', {'status': 'disconnected', 'error': 'Browser has no connected page'})
                    if packet.get('type') != 'frame':
                        continue
                    if time.monotonic() < next_frame:
                        continue
                    next_frame = time.monotonic() + 0.2
                    data = packet.get('data')
                    sequence += 1
                    if not isinstance(data, str) or len(data) > MAX_FILE:
                        raise VisualError('Browser returned an invalid frame', 502)
                    raw = base64.b64decode(data, validate=True)
                    mime = 'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else 'image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else None
                    if mime is None:
                        raise VisualError('Browser frame is not JPEG or PNG', 502)
                    metadata = packet.get('metadata', {})
                    width, height = metadata.get('deviceWidth', 0), metadata.get('deviceHeight', 0)
                    if any(type(d) not in (int, float) or not 0 < d <= 16384 for d in (width, height)):
                        raise VisualError('Browser frame dimensions are invalid', 502)
                    self.validate_live(workspace, live)
                    live['status'] = 'live'
                    emit('status', {'status': 'live'})
                    emit('frame', {'seq': sequence, 'data': data, 'mime': mime, 'width': width, 'height': height})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            live['status'] = 'stale' if isinstance(error, VisualError) and error.status == 409 else 'disconnected'
            live['error'] = str(error) if isinstance(error, VisualError) else 'Browser stream disconnected; reopen the view to retry'
            try:
                emit('status', {'status': live['status'], 'error': live['error']})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            if live['status'] in ('connecting', 'live'):
                live['status'] = 'disconnected'
            with self.lock:
                self.streaming.discard(attachment)


def send_bytes(handler, data, mime, filename=None, csp=None):
    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.send_header('Content-Security-Policy', csp or "sandbox; default-src 'none'; frame-ancestors 'self'")
    if filename:
        handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass


def get_route(handler, visual, public_origin):
    parsed = urlsplit(handler.path)
    if parsed.path.startswith('/visual-preview/'):
        expected_host = public_origin.removeprefix('https://') if public_origin else None
        if handler.headers.get('Host') not in {f'127.0.0.1:{handler.server.server_port}', expected_host}:
            raise VisualError('Use the private Desk URL', 403)
        parts = parsed.path.split('/', 3)
        if len(parts) != 4 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', parts[2]):
            raise VisualError('Preview not found', 404)
        token, name = parts[2], unquote(parts[3])
        data, mime = visual.preview(token, name)
        origin = public_origin if handler.headers.get('Host') == expected_host else f'http://127.0.0.1:{handler.server.server_port}'
        prefix = origin + '/visual-preview/' + token + '/'
        csp = (f"sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' {prefix}; "
               f"style-src 'unsafe-inline' {prefix}; img-src data: blob: {prefix}; font-src {prefix}; "
               "connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
        send_bytes(handler, data, mime, csp=csp)
        return True
    if not parsed.path.startswith('/api/visual/'):
        return False
    if not handler.local_request() or handler.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise VisualError('Cross-origin visual requests are not allowed', 403)
    query = parse_qs(parsed.query, keep_blank_values=True)
    def args(required):
        if set(query) != set(required) or any(len(values) != 1 or not values[0] for values in query.values()):
            raise VisualError('Invalid visual query fields')
        return {key: value[0] for key, value in query.items()}
    if parsed.path == '/api/visual/workspace':
        handler.send(200, visual.list(**args({'tab'})))
    elif parsed.path in ('/api/visual/content', '/api/visual/download'):
        download = parsed.path.endswith('/download')
        data, mime, filename = visual.content(**args({'workspace', 'version'}), download=download)
        send_bytes(handler, data, mime, filename if download else None)
    elif parsed.path == '/api/visual/live':
        visual.stream(handler, **args({'workspace', 'attachment', 'generation'}))
    else:
        raise VisualError('Visual route not found', 404)
    return True


def post_route(handler, visual):
    if not handler.path.startswith('/api/visual/'):
        return False
    methods = {'/api/visual/register': visual.register, '/api/visual/attach': visual.attach, '/api/visual/detach': visual.detach}
    if handler.path not in methods:
        raise VisualError('Visual route not found', 404)
    size = int(handler.headers.get('Content-Length', '0'))
    limit = MAX_BODY if handler.path.endswith('/register') else 8192
    if handler.headers.get_content_type() != 'application/json' or not 0 < size <= limit:
        raise VisualError('Send a bounded JSON visual request')
    body = json.loads(handler.rfile.read(size))
    handler.send(200, methods[handler.path](body))
    return True
