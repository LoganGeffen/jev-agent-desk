import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from visual_workspace import VisualError, fields, process_stamp, safe_components, visible


def within(root, path):
    root = Path(root).resolve()
    requested = root / path
    safe_components(requested.parts)
    if requested.resolve() != requested.absolute() or not requested.resolve().is_relative_to(root):
        raise VisualError('App directory must stay inside its managed workspace without symlinks')
    if not requested.is_dir():
        raise VisualError('App working directory does not exist')
    return requested


def owns_listener(pid, port):
    pending, inodes = [str(pid)], set()
    while pending:
        current = pending.pop()
        proc = Path('/proc') / current
        try:
            pending.extend((proc / 'task' / current / 'children').read_text().split())
            for fd in (proc / 'fd').iterdir():
                try:
                    link = os.readlink(fd)
                    if link.startswith('socket:['):
                        inodes.add(link[8:-1])
                except OSError:
                    pass
        except OSError:
            pass
    for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        cells = row.split()
        if cells[1] == f'0100007F:{port:04X}' and cells[3] == '0A' and cells[9] in inodes:
            return True
    return False


class ManagedApps:
    def __init__(self, playground):
        self.app = playground
        self.path = playground.log.parent / 'apps.json'
        self.lock = threading.RLock()
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        for record in self.records.values():
            if record['status'] == 'starting':
                record.update(status='error', error='Desk restarted during app startup; inspect and stop before starting again')

    def persist(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.records))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def alive(self, record):
        try:
            return process_stamp(record['pid']) == record['source']['process']
        except OSError:
            return False

    def view(self, record):
        if record['status'] in ('ready', 'starting') and not self.alive(record):
            record.update(status='stopped', error='App process ended')
        return {key: record.get(key) for key in ('id', 'title', 'target', 'workspace_id', 'status', 'error', 'log_excerpt', 'page_id', 'run_id', 'source', 'saved_export')}

    def state(self, tab=None, owner=None):
        with self.lock:
            workspace = self.app.visual.workspace(tab) if tab else owner['workspace_id']
            return {'apps': [self.view(record) for record in self.records.values() if record['workspace_id'] == workspace and (owner is None or record['owner'] == owner)]}

    def start(self, owner, workspace, args):
        title = visible(args['title'], 'app title')
        command = args['command']
        if not isinstance(command, list) or not 1 <= len(command) <= 40 or any(not isinstance(item, str) or len(item) > 2000 or '\0' in item for item in command):
            raise VisualError('App command must be bounded executable arguments')
        if not any('{port}' in arg for arg in command):
            raise VisualError('App command must use {port} and bind to 127.0.0.1')
        cwd = within(workspace, args.get('cwd', '.'))
        ready_path = args.get('ready_path', '/')
        if not isinstance(ready_path, str) or not ready_path.startswith('/') or ready_path.startswith('//') or len(ready_path) > 1000:
            raise VisualError('Readiness path must be a local URL path')
        with self.lock:
            if sum(self.alive(record) for record in self.records.values()) >= 2:
                raise VisualError('Two apps are already running; stop one before starting another', 429)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            identifier = uuid.uuid4().hex
            runtime = self.app.log.parent / 'app-homes' / identifier
            runtime.mkdir(parents=True, mode=0o700)
            environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TZ') if key in os.environ}
            environment.update(HOME=str(runtime), PORT=str(port), HOST='127.0.0.1')
            argv = [arg.replace('{port}', str(port)) for arg in command]
            process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            record = {'id': identifier, 'title': title, 'owner': owner, 'target': owner['target'], 'workspace_id': owner['workspace_id'],
                      'status': 'starting', 'pid': process.pid, 'port': port, 'log_excerpt': '', 'error': None, 'saved_export': None,
                      'source': {'cwd': str(cwd), 'process': process_stamp(process.pid), 'command': command,
                                 'build_identity': str(args.get('build_identity') or hashlib.sha256(json.dumps(command).encode()).hexdigest())[:200]}}
            self.records[identifier] = record
            self.persist()
        threading.Thread(target=self.read_output, args=(process, record), daemon=True).start()
        threading.Thread(target=self.ready, args=(record, ready_path), daemon=True).start()
        return self.view(record)

    def read_output(self, process, record):
        while chunk := process.stdout.read1(1024):
            with self.lock:
                record['log_excerpt'] = (record['log_excerpt'] + chunk.decode(errors='replace'))[-4000:]
        process.wait()
        with self.lock:
            if record['status'] != 'stopped':
                record.update(status='error' if process.returncode else 'stopped', error=f'App exited ({process.returncode})')
            self.persist()

    def ready(self, record, path):
        from browser_http import browser_owner
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                if not self.alive(record) or record['status'] != 'starting':
                    return
                owner = browser_owner(self.app.visual, record['target'])
                if owner != record['owner']:
                    raise VisualError('App conversation changed before readiness', 409)
                if owns_listener(record['pid'], record['port']):
                    url = f"http://127.0.0.1:{record['port']}{path}"
                    try:
                        with build_opener(ProxyHandler({})).open(url, timeout=1) as response:
                            if response.status < 400:
                                page = self.app.browser.request('open', owner=owner, profile_id='app-' + record['id'], url=url)
                                if browser_owner(self.app.visual, record['target']) != owner:
                                    raise VisualError('App conversation changed during readiness', 409)
                                with self.lock:
                                    record.update(status='ready', page_id=page['page_id'], run_id=page['run_id'])
                                    self.persist()
                                return
                    except (HTTPError, URLError, TimeoutError):
                        pass
                time.sleep(.1)
            raise VisualError('App did not serve a successful response on its owned loopback port within 20 seconds')
        except Exception as error:
            with self.lock:
                record.update(status='error', error=str(error))
                self.persist()

    def stop(self, owner, app_id):
        from browser_http import browser_owner
        with self.lock:
            record = self.records.get(app_id)
            if record is None or record['owner'] != owner:
                raise VisualError('App does not belong to this conversation', 409)
            if browser_owner(self.app.visual, owner['target']) != owner:
                raise VisualError('App owner changed', 409)
            if self.alive(record) and os.getpgid(record['pid']) == record['pid']:
                os.killpg(record['pid'], signal.SIGTERM)
            if record.get('page_id'):
                pages = self.app.browser.request('pages', owner=owner)['pages']
                for page in pages:
                    if page['profile_id'] == 'app-' + app_id and page['status'] != 'closed':
                        self.app.browser.request('action', owner=owner, page_id=page['page_id'], run_id=page['run_id'], account_epoch=page['account_epoch'], action='close')
                self.app.browser.request('stop_profile', profile_id='app-' + app_id)
            record.update(status='stopped')
            self.persist()
            return self.view(record)


def get_route(handler, apps):
    parsed = urlsplit(handler.path)
    if parsed.path != '/api/apps/state':
        return False
    if not handler.local_request() or handler.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise VisualError('Cross-origin app requests are not allowed', 403)
    query = parse_qs(parsed.query)
    if set(query) != {'tab'} or len(query['tab']) != 1:
        raise VisualError('An exact tab is required')
    handler.send(200, apps.state(tab=query['tab'][0]))
    return True


def post_route(handler, apps):
    if handler.path != '/api/apps/stop':
        return False
    size = int(handler.headers.get('Content-Length', '0'))
    if handler.headers.get_content_type() != 'application/json' or not 0 < size <= 8192:
        raise VisualError('Send a bounded app request')
    body = json.loads(handler.rfile.read(size))
    fields(body, {'target', 'app_id'})
    from browser_http import browser_owner
    handler.send(200, apps.stop(browser_owner(apps.app.visual, body['target']), body['app_id']))
    return True
