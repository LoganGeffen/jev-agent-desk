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

from app_runtime_identity import listener_identity, listener_matches
from visual import read_file
from visual_workspace import MAX_VERSION, MAX_FILES, VisualError, fields, process_stamp, safe_components, visible


def within(root, path):
    root = Path(root).resolve()
    requested = root / path
    safe_components(requested.parts)
    if requested.resolve() != requested.absolute() or not requested.resolve().is_relative_to(root):
        raise VisualError('App directory must stay inside its managed workspace without symlinks')
    if not requested.is_dir():
        raise VisualError('App working directory does not exist')
    return requested


def build_identity(cwd, command):
    digest = hashlib.sha256(json.dumps(command).encode())
    total = count = 0
    for directory, dirs, names in os.walk(cwd, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith('.') and name not in ('node_modules', '__pycache__'))
        for name in sorted(names):
            path = Path(directory) / name
            if name.startswith('.') or path.is_symlink():
                continue
            try:
                safe_components(path.parts)
            except VisualError:
                continue
            data = read_file(path)
            count += 1
            total += len(data)
            if count > MAX_FILES or total > MAX_VERSION:
                raise VisualError('App source fingerprint exceeds capture bounds; provide an exact build_identity')
            digest.update(str(path.relative_to(cwd)).encode() + b'\0' + data)
    return 'sha256:' + digest.hexdigest()


def owns_listener(pid, port):
    return listener_identity(pid, port) is not None


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
            state = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
            return state != 'Z' and process_stamp(record['pid']) == record['source']['process']
        except OSError:
            return False

    def view(self, record):
        if record['status'] in ('ready', 'starting') and not self.alive(record):
            record.update(status='stopped', error='App process ended')
        if record['status'] == 'ready' and not listener_matches(record.get('listener')):
            record.update(status='error', error='App listener ownership changed; browser preview stopped')
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
        identity = str(args.get('build_identity') or build_identity(cwd, command))[:200]
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
                                 'build_identity': identity}}
            self.records[identifier] = record
            self.persist()
        threading.Thread(target=self.read_output, args=(process, record), daemon=True).start()
        threading.Thread(target=self.ready, args=(record, ready_path), daemon=True).start()
        return self.view(record)

    def read_output(self, process, record):
        with process.stdout:
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
                binding = listener_identity(record['pid'], record['port'])
                if binding:
                    url = f"http://127.0.0.1:{record['port']}{path}"
                    try:
                        with build_opener(ProxyHandler({})).open(url, timeout=1) as response:
                            if response.status < 400:
                                if not listener_matches(binding):
                                    raise VisualError('App listener changed during readiness', 409)
                                record['listener'] = binding
                                self.app.browser.request('bind_app', owner=owner, profile_id='app-' + record['id'], binding=binding)
                                page = self.app.browser.request('open', owner=owner, profile_id='app-' + record['id'], url=url)
                                if browser_owner(self.app.visual, record['target']) != owner:
                                    raise VisualError('App conversation changed during readiness', 409)
                                with self.lock:
                                    record.update(page_id=page['page_id'], run_id=page['run_id'])
                                    try:
                                        self.snapshot(record, page, 'initial')
                                    except Exception as error:
                                        record['error'] = 'App is running, but its saved screenshot failed: ' + str(error)
                                    record['status'] = 'ready'
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

    def snapshot(self, record, page, revision):
        result = self.app.browser.request('action', owner=record['owner'], page_id=page['page_id'], run_id=page['run_id'], account_epoch=page['account_epoch'], action='screenshot')['result']
        body = {'target': record['target'], 'title': record['title'] + ' — saved screenshot', 'kind': 'image', 'mode': 'captured',
                'entrypoint': 'app.jpg', 'files': [{'path': 'app.jpg', 'data': result['data']}],
                'receipt_key': 'app-' + record['id'] + '-' + revision}
        if record.get('saved_export'):
            body['creation_id'] = record['saved_export']['creation_id']
        record['saved_export'] = self.app.visual.register(body, app_source={'id': record['id'], **record['source'], 'representation': 'screenshot'})

    def stop(self, owner, app_id):
        from browser_http import browser_owner
        with self.lock:
            record = self.records.get(app_id)
            if record is None or record['owner'] != owner:
                raise VisualError('App does not belong to this conversation', 409)
            if browser_owner(self.app.visual, owner['target']) != owner:
                raise VisualError('App owner changed', 409)
            errors = []
            if record.get('page_id'):
                try:
                    pages = self.app.browser.request('pages', owner=owner)['pages']
                    for page in pages:
                        if page['profile_id'] == 'app-' + app_id and page['status'] != 'closed':
                            try:
                                if page['page_id'] == record['page_id']:
                                    self.snapshot(record, page, 'stopped')
                            except Exception as error:
                                errors.append('Final screenshot was not saved: ' + str(error))
                            self.app.browser.request('action', owner=owner, page_id=page['page_id'], run_id=page['run_id'], account_epoch=page['account_epoch'], action='close')
                except Exception as error:
                    errors.append('App browser cleanup failed: ' + str(error))
                try:
                    self.app.browser.request('stop_profile', profile_id='app-' + app_id)
                except Exception as error:
                    errors.append('App profile cleanup failed: ' + str(error))
            if browser_owner(self.app.visual, owner['target']) != owner:
                raise VisualError('App owner changed before stop', 409)
            if self.alive(record) and os.getpgid(record['pid']) == record['pid']:
                os.killpg(record['pid'], signal.SIGTERM)
                deadline = time.monotonic() + 2
                while self.alive(record) and time.monotonic() < deadline:
                    time.sleep(.02)
                if self.alive(record) and os.getpgid(record['pid']) == record['pid']:
                    os.killpg(record['pid'], signal.SIGKILL)
            record['error'] = '; '.join(errors) or None
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
