import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError


class BrowserError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def stamp(pid):
    return str(pid) + ':' + Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def owner_key(owner):
    required = {'workspace_id', 'target', 'provider', 'thread_id', 'process', 'agent_process'}
    if not isinstance(owner, dict) or set(owner) != required or not all(owner.values()):
        raise BrowserError('An exact managed conversation owner is required', 400)
    if not isinstance(owner['target'], dict) or set(owner['target']) != {'tab', 'pane', 'identity'}:
        raise BrowserError('An exact managed conversation target is required', 400)
    return hashlib.sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest()


def valid_url(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise BrowserError('A browser URL is required', 400)
    parsed = urlsplit(url)
    if url != 'about:blank' and (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password):
        raise BrowserError('Use an HTTP or HTTPS URL without embedded credentials', 400)
    return url


def write_private(path, value):
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


class BrowserRuntime:
    def __init__(self, directory, max_profiles=2, lease_seconds=20, idle_seconds=1200):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.max_profiles = max_profiles
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.profiles = {}
        self.pages = {}
        self.history = []
        self.launch_lock = asyncio.Lock()
        self.state_path = self.directory / 'ownership.json'
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text())
            self.history = [{**page, 'status': 'stale', 'error': 'Browser restarted; prior controls cannot resume'} for page in saved.get('pages', [])]
            for profile in saved.get('profiles', []):
                self.profiles[profile['profile_id']] = self.profile_record(profile['profile_id'], profile['account_label'])
                self.profiles[profile['profile_id']]['epoch'] = profile.get('account_epoch', 0)
                if profile.get('status') in ('human', 'waiting', 'pausing'):
                    self.profiles[profile['profile_id']]['mode'] = 'waiting'
        self.playwright = None
        self.watchdog = None

    def profile_record(self, profile_id, label):
        return {'id': profile_id, 'label': label, 'context': None, 'run': None,
                'lock': asyncio.Lock(), 'mode': 'agent', 'lease': None, 'epoch': 0,
                'last_used': time.monotonic()}

    async def start(self):
        self.playwright = await async_playwright().start()
        self.watchdog = asyncio.create_task(self.expire_leases())
        return self

    def profile_view(self, profile):
        mode = profile['mode']
        return {'profile_id': profile['id'], 'account_label': profile['label'],
                'run_id': profile['run'], 'account_epoch': profile['epoch'],
                'status': mode if mode != 'agent' else ('ready' if profile['context'] else 'stopped')}

    def receipt(self, record):
        profile = record['profile']
        page = record['page']
        status = 'closed' if page.is_closed() else profile['mode']
        return {**self.profile_view(profile), 'page_id': record['id'], 'run_id': record['run'],
                'status': 'ready' if status == 'agent' else status, 'url': page.url,
                'title': record.get('title', ''), 'target': record['owner']['target'],
                'workspace_id': record['owner']['workspace_id'], 'parent_page_id': record.get('parent'),
                'owner': record['owner']}

    def save(self):
        records = [{**self.receipt(page), 'url': '', 'title': ''} for page in self.pages.values()]
        write_private(self.state_path, {'profiles': [self.profile_view(p) for p in self.profiles.values()],
                                       'pages': (self.history + records)[-128:]})

    async def get_profile(self, profile_id, label=None):
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', profile_id):
            raise BrowserError('Invalid account profile', 400)
        async with self.launch_lock:
            if label is not None and (not isinstance(label, str) or not 1 <= len(label.strip()) <= 120 or any(ord(c) < 32 for c in label)):
                raise BrowserError('Use a visible account label up to 120 characters', 400)
            profile = self.profiles.get(profile_id)
            if profile is None:
                if len(self.profiles) >= 32:
                    raise BrowserError('Saved profile limit reached', 429)
                profile = self.profile_record(profile_id, label or ('Desk account' if profile_id == 'desk' else profile_id))
                self.profiles[profile_id] = profile
            if profile['context'] is not None:
                return profile
            running = [p for p in self.profiles.values() if p['context'] is not None]
            if len(running) >= self.max_profiles:
                idle = [p for p in running if p['mode'] == 'agent' and not p['lock'].locked() and
                        not any(r['profile'] is p and not r['page'].is_closed() for r in self.pages.values())]
                if not idle:
                    raise BrowserError('Waiting for a browser slot; active pages and sign-in are preserved', 429)
                await self.stop_profile(min(idle, key=lambda p: p['last_used']))
            profile_dir = self.directory / 'profiles' / profile_id
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            context = await self.playwright.chromium.launch_persistent_context(
                str(profile_dir), headless=True, viewport={'width': 1280, 'height': 800},
                accept_downloads=True, args=['--disable-extensions', '--disable-sync', '--no-first-run', '--no-default-browser-check'])
            context.set_default_timeout(10000)
            profile['context'] = context
            profile['run'] = str(uuid.uuid4())
            context.on('close', lambda: self.context_closed(profile, context))
            for page in context.pages:
                await page.close()
            self.save()
            return profile

    def context_closed(self, profile, context):
        if profile['context'] is context:
            profile['context'] = None
            if profile['mode'] != 'agent':
                profile['mode'] = 'waiting'
                profile['lease'] = None
            self.save()

    def add_page(self, page, owner, profile, parent=None):
        record = {'id': str(uuid.uuid4()), 'run': profile['run'], 'owner': owner,
                  'key': owner_key(owner), 'page': page, 'profile': profile, 'parent': parent}
        self.pages[record['id']] = record
        page.on('popup', lambda popup: self.add_page(popup, record['owner'], profile, record['id']))
        page.on('close', self.save)
        page.on('download', lambda download: asyncio.create_task(self.download(record, download)))
        self.save()
        return record

    async def download(self, record, download):
        try:
            failure = await download.failure()
            if failure or record['profile']['mode'] != 'agent':
                return
            path = await download.path()
            if record['profile']['mode'] != 'agent':
                return
            if path is None or path.stat().st_size > 8 * 1024 * 1024:
                record['download_error'] = 'Download exceeds the capture limit or is unavailable'
                return
            download_id = str(uuid.uuid4())
            record.setdefault('downloads', []).append({'download_id': download_id, 'receipt_key': 'download-' + download_id,
                'name': download.suggested_filename, 'data': base64.b64encode(path.read_bytes()).decode(),
                'page_id': record['id'], 'run_id': record['run']})
            record['downloads'] = record['downloads'][-4:]
        except (PlaywrightError, OSError):
            record['download_error'] = 'Download failed before completion'

    def owned(self, args, allow_closed=False):
        key = owner_key(args.get('owner'))
        record = self.pages.get(args.get('page_id'))
        if record is None or record['key'] != key or record['run'] != args.get('run_id'):
            raise BrowserError('Browser page ownership or run changed')
        if record['profile']['run'] != record['run'] or record['profile']['context'] is None:
            raise BrowserError('Browser run ended; old controls cannot resume')
        if record['page'].is_closed() and not allow_closed:
            raise BrowserError('That browser page is closed')
        return record

    def agent_ready(self, profile):
        if profile['mode'] != 'agent':
            raise BrowserError('Waiting while you sign in; agent inspection and controls are paused', 423)

    def lease_valid(self, args, record):
        profile = record['profile']
        lease = profile['lease']
        if profile['mode'] != 'human' or not lease or lease['id'] != args.get('lease_id') or lease['client'] != args.get('client_id') or lease['owner'] != record['key']:
            raise BrowserError('Human browser control expired or changed', 423)
        if lease['until'] <= time.monotonic():
            profile['lease'] = None
            profile['mode'] = 'waiting'
            self.save()
            raise BrowserError('Human browser control expired; automation remains paused', 423)
        return lease

    async def expire_leases(self):
        while True:
            await asyncio.sleep(1)
            for profile in self.profiles.values():
                lease = profile['lease']
                if lease and lease['until'] <= time.monotonic():
                    profile['lease'] = None
                    profile['mode'] = 'waiting'
                    self.save()
                elif profile['context'] and profile['mode'] == 'agent' and not profile['lock'].locked() and time.monotonic() - profile['last_used'] >= self.idle_seconds:
                    async with profile['lock']:
                        await self.stop_profile(profile)

    async def request(self, op, _guard=None, **args):
        async def guard():
            if _guard is not None and not await _guard(args.get('owner')):
                raise BrowserError('Conversation identity changed before browser action')
        if args.get('owner'):
            await guard()
        if op == 'profiles':
            return {'profiles': [self.profile_view(p) for p in self.profiles.values()]}
        if op in ('pages', 'view_pages'):
            key = owner_key(args.get('owner'))
            owned_pages = [p for p in self.pages.values() if p['key'] == key]
            if op == 'pages':
                for page in owned_pages:
                    self.agent_ready(page['profile'])
            current = [self.receipt(p) for p in owned_pages]
            old = [p for p in self.history if owner_key(p['owner']) == key]
            return {'pages': current + old}
        if op == 'rebind':
            previous, owner = args.get('previous_owner'), args.get('owner')
            old_key, new_key = owner_key(previous), owner_key(owner)
            if any(previous[k] != owner[k] for k in ('workspace_id', 'provider', 'thread_id')):
                raise BrowserError('Only a validated resume of the same conversation can reclaim pages')
            changed = []
            matching = [record for record in self.pages.values() if record['key'] == old_key and not record['page'].is_closed()]
            if any(record['profile']['mode'] != 'agent' or record['profile']['lock'].locked() for record in matching):
                raise BrowserError('Finish browser activity and human sign-in before resuming ownership', 423)
            ids = {record['id']: str(uuid.uuid4()) for record in matching}
            for page_id, record in list(self.pages.items()):
                if record['key'] != old_key or record['page'].is_closed():
                    continue
                if record['profile']['mode'] != 'agent' or record['profile']['lock'].locked():
                    raise BrowserError('Finish browser activity and human sign-in before resuming ownership', 423)
                del self.pages[page_id]
                record.update(id=ids[page_id], owner=owner, key=new_key, parent=ids.get(record.get('parent'), record.get('parent')))
                self.pages[record['id']] = record
                changed.append(self.receipt(record))
            self.save()
            return {'pages': changed}
        if op in ('downloads', 'ack_download'):
            key = owner_key(args.get('owner'))
            records = [r for r in self.pages.values() if r['key'] == key]
            for record in records:
                self.agent_ready(record['profile'])
            if op == 'downloads':
                return {'downloads': [d for r in records for d in r.get('downloads', [])],
                        'errors': [r['download_error'] for r in records if r.get('download_error')]}
            for record in records:
                record['downloads'] = [d for d in record.get('downloads', []) if d['download_id'] != args.get('download_id')]
            return {'acknowledged': True}
        if op == 'open':
            owner = args.get('owner')
            owner_key(owner)
            url = valid_url(args.get('url', 'about:blank'))
            profile = await self.get_profile(args.get('profile_id', 'desk'), args.get('account_label'))
            self.agent_ready(profile)
            async with profile['lock']:
                self.agent_ready(profile)
                await guard()
                if sum(not p['page'].is_closed() for p in self.pages.values() if p['profile'] is profile) >= 12:
                    raise BrowserError('This account has reached its 12 page limit', 429)
                page = await profile['context'].new_page()
                record = self.add_page(page, owner, profile)
                await guard()
                if url != 'about:blank':
                    await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                record['title'] = await page.title()
                self.save()
                return self.receipt(record)
        if op == 'stop_profile':
            profile = self.profiles.get(args.get('profile_id'))
            if profile is None:
                raise BrowserError('Unknown account profile', 404)
            if profile['mode'] != 'agent':
                raise BrowserError('Finish human sign-in before stopping this profile', 423)
            async with profile['lock']:
                await self.stop_profile(profile)
            return self.profile_view(profile)
        record = self.owned(args)
        profile, page = record['profile'], record['page']
        profile['last_used'] = time.monotonic()
        if op == 'lease':
            client = args.get('client_id')
            if not isinstance(client, str) or not 1 <= len(client) <= 120:
                raise BrowserError('A device identity is required', 400)
            if profile['mode'] in ('human', 'pausing'):
                raise BrowserError('Another device currently controls this account', 423)
            profile['mode'] = 'pausing'
            self.save()
            try:
                async with profile['lock']:
                    await guard()
                    self.owned(args)
                    profile['mode'] = 'human'
                    profile['lease'] = {'id': secrets.token_urlsafe(24), 'client': client, 'owner': record['key'],
                                        'sequence': 0, 'until': time.monotonic() + self.lease_seconds}
                    self.save()
                    return {**self.receipt(record), 'lease_id': profile['lease']['id'], 'client_id': client, 'expires_in': self.lease_seconds}
            except BaseException:
                profile['mode'], profile['lease'] = 'waiting', None
                self.save()
                raise
        if op == 'frame':
            self.owned(args)
            data = await page.screenshot(type='jpeg', quality=65, timeout=5000)
            self.owned(args)
            return {'page_id': record['id'], 'run_id': record['run'], 'mime': 'image/jpeg',
                    'data': base64.b64encode(data).decode(), 'width': 1280, 'height': 800,
                    'status': self.receipt(record)['status']}
        if op in ('input', 'done', 'disconnect', 'heartbeat'):
            self.lease_valid(args, record)
            async with profile['lock']:
                await guard()
                self.owned(args)
                lease = self.lease_valid(args, record)
                if op == 'heartbeat':
                    lease['until'] = time.monotonic() + self.lease_seconds
                    return {'expires_in': self.lease_seconds}
                if op == 'disconnect':
                    profile['mode'], profile['lease'] = 'waiting', None
                    self.save()
                    return self.receipt(record)
                if op == 'done':
                    record['title'] = await page.title()
                    await guard()
                    self.lease_valid(args, record)
                    profile['epoch'] += 1
                    profile['mode'], profile['lease'] = 'agent', None
                    self.save()
                    return {**self.receipt(record), 'account_status': 'unknown', 'message': 'Browser control returned. Inspect the refreshed page to confirm account state.'}
                sequence = args.get('sequence')
                if type(sequence) is not int or sequence <= lease['sequence']:
                    raise BrowserError('Stale human input was rejected; it will not be replayed')
                lease['sequence'] = sequence
                lease['until'] = time.monotonic() + self.lease_seconds
                await self.human_input(page, args.get('action'), args.get('args', {}))
                return {'accepted': True, 'sequence': sequence, 'page_id': record['id'], 'run_id': record['run']}
        if op != 'action':
            raise BrowserError('Unknown browser operation', 400)
        self.agent_ready(profile)
        async with profile['lock']:
            await guard()
            self.owned(args)
            self.agent_ready(profile)
            action = args.get('action')
            if action != 'snapshot' and args.get('account_epoch') != profile['epoch']:
                raise BrowserError('Account state changed; inspect the page before another action')
            result = await self.agent_action(record, action, args.get('args', {}), guard)
            self.agent_ready(profile)
            if action != 'close':
                self.owned(args)
                record['title'] = await page.title()
            self.save()
            return {**self.receipt(record), 'result': result}

    async def agent_action(self, record, action, args, guard):
        page = record['page']
        if not isinstance(args, dict):
            raise BrowserError('Browser action arguments must be an object', 400)
        if action == 'navigate':
            await page.goto(valid_url(args.get('url')), wait_until='domcontentloaded', timeout=20000)
        elif action == 'snapshot':
            text = (await page.locator('body').inner_text())[:16000]
            elements = await page.locator('a,button,input,select,textarea').evaluate_all('els => els.slice(0,100).map(e => ({tag:e.tagName.toLowerCase(),text:(e.innerText || e.getAttribute("aria-label") || "").slice(0,120),id:e.id,name:e.getAttribute("name"),type:e.getAttribute("type"),href:e.tagName === "A" ? e.getAttribute("href") : undefined}))')
            return {'text': text, 'elements': elements, 'account_epoch': record['profile']['epoch']}
        elif action == 'click':
            locator = page.locator(self.selector(args))
            await locator.wait_for(state='visible')
            await guard()
            await locator.click(timeout=1000)
        elif action == 'fill':
            text = args.get('text')
            if not isinstance(text, str) or len(text) > 16000:
                raise BrowserError('Fill text exceeds the browser limit', 400)
            locator = page.locator(self.selector(args))
            await locator.wait_for(state='visible')
            await guard()
            await locator.fill(text, timeout=1000)
        elif action == 'press':
            await page.keyboard.press(self.key(args))
        elif action == 'scroll':
            await page.mouse.wheel(*self.coordinates(args, scroll=True))
        elif action in ('back', 'forward', 'reload'):
            await {'back': page.go_back, 'forward': page.go_forward, 'reload': page.reload}[action](wait_until='domcontentloaded')
        elif action == 'close':
            await page.close()
        elif action == 'screenshot':
            return {'mime': 'image/jpeg', 'data': base64.b64encode(await page.screenshot(type='jpeg', quality=65)).decode()}
        elif action == 'downloads':
            return {'downloads': record.get('downloads', []), 'error': record.get('download_error')}
        else:
            raise BrowserError('Unsupported browser action', 400)
        return {'accepted': True}

    def selector(self, args):
        selector = args.get('selector')
        if not isinstance(selector, str) or not 1 <= len(selector) <= 2000:
            raise BrowserError('A bounded page selector is required', 400)
        return selector

    def key(self, args):
        key = args.get('key')
        if not isinstance(key, str) or not 1 <= len(key) <= 80:
            raise BrowserError('A browser key is required', 400)
        return key

    def coordinates(self, args, scroll=False):
        x, y = args.get('x'), args.get('y')
        limit = 10000 if scroll else 1280
        if type(x) not in (int, float) or type(y) not in (int, float) or not abs(x) <= limit or not abs(y) <= limit or (not scroll and (x < 0 or y < 0 or y > 800)):
            raise BrowserError('Input coordinates are outside the browser viewport', 400)
        return x, y

    async def human_input(self, page, action, args):
        if action == 'click':
            await page.mouse.click(*self.coordinates(args))
        elif action == 'text':
            text = args.get('text')
            if not isinstance(text, str) or len(text) > 4096:
                raise BrowserError('Human text input exceeds the browser limit', 400)
            await page.keyboard.insert_text(text)
        elif action == 'key':
            await page.keyboard.press(self.key(args))
        elif action == 'scroll':
            await page.mouse.wheel(*self.coordinates(args, scroll=True))
        else:
            raise BrowserError('Unsupported human input', 400)

    async def stop_profile(self, profile):
        if profile['context']:
            await profile['context'].close()
        self.save()

    async def close(self):
        if self.watchdog:
            self.watchdog.cancel()
        for profile in self.profiles.values():
            await self.stop_profile(profile)
        if self.playwright:
            await self.playwright.stop()


class ManagedBrowser:
    def __init__(self, run_dir):
        self.directory = Path(run_dir).resolve() / 'browser'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.record_path = self.directory / 'service.json'
        digest = hashlib.sha256(str(self.directory).encode()).hexdigest()[:20]
        socket_dir = Path('/tmp') / f'jev-browser-{os.getuid()}'
        socket_dir.mkdir(mode=0o700, exist_ok=True)
        if socket_dir.stat().st_uid != os.getuid() or socket_dir.stat().st_mode & 0o077:
            raise BrowserError('Browser service socket directory is not private')
        self.socket_path = socket_dir / (digest + '.sock')
        self.start_lock = threading.Lock()
        self.record = None
        self.process = None
        self.validate_owner = None

    def valid_record(self, record):
        try:
            return (record['process'] == stamp(record['pid']) and record['boot'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                    and record['socket_inode'] == self.socket_path.stat().st_ino)
        except (OSError, KeyError, ValueError):
            return False

    def ensure_service(self):
        with self.start_lock:
            if self.record and self.valid_record(self.record):
                return
            with open(self.directory / 'launch.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self.record_path.exists():
                    candidate = json.loads(self.record_path.read_text())
                    if self.valid_record(candidate):
                        self.record = candidate
                        return
                env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'PLAYWRIGHT_BROWSERS_PATH') if key in os.environ}
                with open(self.directory / 'service.log', 'ab') as log:
                    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--service', str(self.directory), '--socket', str(self.socket_path)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
                self.process = process
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise BrowserError('Managed browser service failed to start', 503)
                    if self.record_path.exists():
                        candidate = json.loads(self.record_path.read_text())
                        if candidate.get('pid') == process.pid and self.valid_record(candidate):
                            self.record = candidate
                            return
                    time.sleep(.05)
                raise BrowserError('Managed browser service startup timed out', 503)

    def request(self, op, **args):
        self.ensure_service()
        record = self.record
        if not self.valid_record(record):
            raise BrowserError('Managed browser service identity changed')
        payload = json.dumps({'token': record['token'], 'service_id': record['service_id'], 'op': op, 'args': args}).encode() + b'\n'
        if len(payload) > 128 * 1024:
            raise BrowserError('Browser request exceeds the input limit', 400)
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(35)
                connection.connect(str(self.socket_path))
                connection.sendall(payload)
                with connection.makefile('rb') as file:
                    while True:
                        data = file.readline(24 * 1024 * 1024 + 1)
                        if not data or len(data) > 24 * 1024 * 1024:
                            break
                        response = json.loads(data)
                        if 'validate_owner' not in response:
                            break
                        valid = response.get('service_id') == record['service_id'] and response['validate_owner'] == args.get('owner')
                        if valid and self.validate_owner:
                            try:
                                valid = bool(self.validate_owner(response['validate_owner']))
                            except Exception:
                                valid = False
                        connection.sendall(json.dumps({'valid': valid}).encode() + b'\n')
            if not data or len(data) > 24 * 1024 * 1024:
                raise BrowserError('Browser response was unavailable; no automatic retry', 503)
            result = json.loads(data)
        except (OSError, ValueError) as error:
            if isinstance(error, BrowserError):
                raise
            raise BrowserError('Browser request outcome is uncertain; no automatic retry', 503) from None
        if result.get('service_id') != record['service_id']:
            raise BrowserError('Browser service identity changed')
        if 'error' in result:
            raise BrowserError(result['error'], result.get('status', 409))
        return result['result']

    def close(self):
        pass

    def shutdown(self):
        result = self.request('shutdown')
        self.record = None
        if self.process:
            self.process.wait(timeout=15)
            self.process = None
        return result


async def serve(directory, socket_path):
    os.umask(0o077)
    directory, socket_path = Path(directory), Path(socket_path)
    lock = open(directory / 'service.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    socket_path.unlink(missing_ok=True)
    runtime = await BrowserRuntime(directory).start()
    token, service_id = secrets.token_urlsafe(32), str(uuid.uuid4())
    stop = asyncio.Event()

    async def handle(reader, writer):
        try:
            data = await asyncio.wait_for(reader.readline(), 5)
            request = json.loads(data)
            if not secrets.compare_digest(str(request.get('token')), token) or request.get('service_id') != service_id:
                raise BrowserError('Browser service identity changed', 403)
            if request.get('op') == 'shutdown':
                result = {'stopped': True}
                stop.set()
            else:
                async def guard(owner):
                    writer.write(json.dumps({'service_id': service_id, 'validate_owner': owner}).encode() + b'\n')
                    await writer.drain()
                    return json.loads(await asyncio.wait_for(reader.readline(), 5)).get('valid') is True
                result = await asyncio.wait_for(runtime.request(request['op'], _guard=guard, **request.get('args', {})), 30)
            response = {'result': result}
        except BrowserError as error:
            response = {'error': str(error), 'status': error.status}
        except (PlaywrightError, asyncio.TimeoutError):
            response = {'error': 'Browser operation failed or timed out; no automatic retry', 'status': 409}
        except Exception:
            response = {'error': 'Browser request failed; no automatic retry', 'status': 400}
        try:
            writer.write(json.dumps({**response, 'service_id': service_id}).encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, str(socket_path), limit=128 * 1024)
    write_private(directory / 'service.json', {'pid': os.getpid(), 'process': stamp(os.getpid()),
        'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'socket_inode': socket_path.stat().st_ino,
        'token': token, 'service_id': service_id})
    try:
        async with server:
            await stop.wait()
    finally:
        await runtime.close()
        socket_path.unlink(missing_ok=True)
        (directory / 'service.json').unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--service', required=True)
    parser.add_argument('--socket', required=True)
    options = parser.parse_args()
    asyncio.run(serve(options.service, options.socket))
