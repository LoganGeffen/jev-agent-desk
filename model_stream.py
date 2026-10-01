import atexit
from contextlib import contextmanager
import json
import os
import select
import subprocess
import tempfile
import threading
import time


class ModelConnection:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='jev-answer-')
        self.errors = tempfile.TemporaryFile()
        self.pending = b''
        self.sequence = 0
        command = ['codex', 'app-server', '-c', 'project_doc_max_bytes=0',
                   '-c', 'agents.enabled=false', '-c', 'web_search=disabled',
                   '--disable', 'shell_tool', '--disable', 'apps', '--disable', 'plugins',
                   '-c', 'mcp_servers={}']
        self.process = subprocess.Popen(command, cwd=self.directory.name, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=self.errors,
                                       env={k: v for k, v in os.environ.items()
                                            if k not in ('TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY')})
        atexit.register(self.close)
        self.deadline = time.monotonic() + 20
        try:
            self.call('initialize', {'clientInfo': {'name': 'jev_readback', 'version': '1'}})
            self.send('initialized', {})
            config = self.call('config/read', {'includeLayers': False})['config']
            self.overrides = {'mcp_servers.' + name + '.enabled': False
                              for name in config.get('mcp_servers', {})}
        except BaseException:
            self.close()
            raise

    def send(self, method, params, id=None):
        body = {'method': method, 'params': params}
        if id is not None:
            body['id'] = id
        self.process.stdin.write((json.dumps(body) + '\n').encode())
        self.process.stdin.flush()

    def receive(self):
        while b'\n' not in self.pending:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError('Spoken answer timed out')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Spoken answer connection closed')
            self.pending += chunk
        line, self.pending = self.pending.split(b'\n', 1)
        return json.loads(line)

    def call(self, method, params):
        self.sequence += 1
        self.send(method, params, self.sequence)
        while True:
            event = self.receive()
            if event.get('id') == self.sequence:
                if 'error' in event:
                    raise RuntimeError(event['error']['message'])
                return event['result']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.errors.close()
        self.directory.cleanup()
        atexit.unregister(self.close)


_idle = []
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)


@contextmanager
def connection():
    with _slots:
        with _lock:
            client = _idle.pop() if _idle else None
        client = client or ModelConnection()
        try:
            yield client
        except BaseException:
            client.close()
            raise
        else:
            with _lock:
                _idle.append(client)


def stream_answer(question, evidence, instructions, model, on_text=None):
    started = time.monotonic()
    first_text_ms = None
    text = ''
    provider_error = None
    with connection() as client:
        client.deadline = started + 60
        thread = client.call('thread/start', {'model': model, 'cwd': client.directory.name, 'ephemeral': True,
                             'config': client.overrides,
                             'approvalPolicy': 'never', 'sandbox': 'read-only',
                             'baseInstructions': 'You transform supplied text or answer from supplied evidence. Never use tools.',
                             'developerInstructions': instructions})['thread']['id']
        client.sequence += 1
        client.send('turn/start', {'threadId': thread, 'effort': os.environ.get('JEV_ASK_EFFORT', 'none'),
                    'input': [{'type': 'text', 'text': json.dumps({'question': question, 'evidence': evidence})}]}, client.sequence)
        while True:
            event = client.receive()
            if event.get('id') == client.sequence and 'error' in event:
                raise RuntimeError(event['error']['message'])
            params = event.get('params', {})
            if params.get('threadId') != thread:
                continue
            if event.get('method') == 'item/agentMessage/delta':
                delta = params['delta']
                if first_text_ms is None:
                    first_text_ms = round((time.monotonic() - started) * 1000)
                text += delta
                if on_text:
                    on_text(delta)
            elif event.get('method') == 'error':
                provider_error = params.get('error', {}).get('message')
            elif event.get('method') == 'turn/completed':
                if params['turn']['status'] != 'completed':
                    reason = (params['turn'].get('error') or {}).get('message') or provider_error
                    raise RuntimeError('Spoken answer did not complete' + (': ' + reason if reason else ''))
                break
        if not text.strip():
            raise RuntimeError('Spoken answer returned no text')
        client.call('thread/unsubscribe', {'threadId': thread})
    return {'text': text, 'model': model, 'first_text_ms': first_text_ms,
            'answer_ms': round((time.monotonic() - started) * 1000)}
