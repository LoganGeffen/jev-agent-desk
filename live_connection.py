import fcntl
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import uuid
from urllib.request import urlopen

from managed_provider import read_record, record_path
from pane_identity import UUID
from visual_workspace import agent_process, process_stamp


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return json.load(stream)


def write_private(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


def native_host(native_process):
    pid = native_process.split(':')[0]
    sockets = set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            value = os.readlink(fd)
            if value.startswith('socket:['):
                sockets.add(value[8:-1])
        except OSError:
            pass
    peers = set()
    rows = subprocess.check_output(['ss', '-xnpH'], text=True, timeout=3).splitlines()
    for row in rows:
        columns = row.split()
        if len(columns) >= 9 and columns[7] in sockets and '/codex-daemon-' in columns[4]:
            peers.update(re.findall(r'pid=(\d+)', row))
    if len(peers) > 1:
        raise ValueError('Native MCP host is ambiguous')
    return process_stamp(next(iter(peers))) if peers else native_process


def register(run_dir, target, thread_id, workspace, profile_id='desk'):
    from desk_mcp import Bridge

    run_dir = Path(run_dir).resolve()
    if set(target) != {'tab', 'pane', 'identity'} or not UUID.fullmatch(thread_id):
        raise ValueError('An exact current conversation target and UUID are required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', profile_id) or profile_id.startswith('app-'):
        raise ValueError('Invalid account profile')
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError('The registered workspace must exist')
    origin = (run_dir / 'url').read_text().strip()
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', origin):
        raise ValueError('A loopback Desk service is required')
    with urlopen(origin + '/api/state', timeout=15) as response:
        state = json.load(response)
    panes = [pane for tab in state['tabs'] if tab['id'] == target['tab']
             for pane in tab['panes'] if pane['id'] == target['pane']]
    if len(panes) != 1:
        raise ValueError('That conversation target is no longer present')
    pane = panes[0]
    if (pane.get('identity') != target['identity'] or pane.get('thread_id') != thread_id
            or pane.get('provider') != 'codex' or Path(pane['workspace']).resolve() != workspace):
        raise ValueError('Conversation identity or workspace changed before connection')
    native = agent_process(pane)
    host = native_host(native)
    registry_path = run_dir / 'live-connections.json'
    with open(run_dir / 'live-connections.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = private_json(registry_path) if registry_path.exists() else {}
        previous = registry.get(thread_id)
        if previous:
            record = read_record(run_dir, previous)
            if (record.get('live_native_process') == native and record.get('process') == pane['process']
                    and record.get('live_host_process') == host and record.get('workspace') == str(workspace)
                    and record.get('profile_id') == profile_id and record.get('pane') == target['pane']):
                Bridge(run_dir, previous).call('desk_context', {})
                return {'session_id': previous, 'thread_id': thread_id, 'target': target, 'native_process': native, 'host_process': host}
        identifier = uuid.uuid4().hex
        path = record_path(run_dir, identifier)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {'token': secrets.token_urlsafe(32), 'pid': int(pane['process'].split(':')[0]),
                  'process': pane['process'], 'tab': target['tab'], 'pane': target['pane'], 'provider': 'codex',
                  'workspace': str(workspace), 'profile_id': profile_id, 'resume': thread_id,
                  'live_native_process': native, 'live_host_process': host}
        write_private(path, record)
        try:
            Bridge(run_dir, identifier).call('desk_context', {})
            with closing(sqlite3.connect((run_dir / 'providers.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                owner = json.loads(db.execute('SELECT owner FROM bindings WHERE id=?', (identifier,)).fetchone()[0])
            if (owner['thread_id'] != thread_id or owner['agent_process'] != native
                    or owner['process'] != pane['process'] or owner['target'] != target):
                raise ValueError('Conversation changed during connection')
            record['live_owner'] = owner
            write_private(path, record)
            registry[thread_id] = identifier
            write_private(registry_path, registry)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return {'session_id': identifier, 'thread_id': thread_id, 'target': target, 'native_process': native, 'host_process': host}


def parent_processes():
    pid = os.getppid()
    while pid > 1:
        yield process_stamp(pid)
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        pid = int(stat[1])


def resolve(run_dir, metadata):
    from desk_mcp import Bridge

    thread = metadata.get('threadId') if isinstance(metadata, dict) else None
    if not isinstance(thread, str) or not UUID.fullmatch(thread):
        raise ValueError('Native MCP thread metadata is required')
    if metadata.get('sessionId', thread) != thread:
        raise ValueError('Native MCP session metadata disagrees')
    registry = private_json(Path(run_dir) / 'live-connections.json')
    identifier = registry.get(thread)
    if not identifier:
        raise ValueError('This native conversation is not connected to Desk')
    bridge = Bridge(run_dir, identifier)
    record = bridge.record
    if (record.get('resume') != thread or not record.get('live_owner')
            or record.get('live_host_process') not in set(parent_processes())):
        raise ValueError('Desk connection does not belong to this native MCP host')
    for key in ('process', 'live_native_process', 'live_host_process'):
        stamp = record[key]
        if process_stamp(stamp.split(':')[0]) != stamp:
            raise ValueError('Native conversation process changed; reconnect explicitly')
    bridge.call('desk_context', {})
    return bridge
