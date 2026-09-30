import os
from pathlib import Path
import re
import sqlite3
import subprocess
from contextlib import closing


UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
TITLE_ID = re.compile(r'^([0-9a-f-]{20,36})(?:\.\.\.|…)?(?: [\u2800-\u28ff])?$')
CODEX_IDENTITY_CONFIG = ['-c', 'tui.terminal_title=["session-id"]']


def process_tree(pid, proc=Path('/proc')):
    pending = [str(pid)]
    seen = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        path = proc / current
        yield path
        try:
            pending.extend((path / 'task' / current / 'children').read_text().split())
        except OSError:
            pass


def writer_thread_ids(process, codex_home=None):
    held = set()
    for descriptor in (process / 'fd').iterdir():
        try:
            path = Path(os.readlink(descriptor))
        except OSError:
            continue
        matches = (path.parent == codex_home / 'thread-writer-locks' if codex_home is not None
                   else path.parent.name == 'thread-writer-locks')
        if matches and UUID.fullmatch(path.stem):
            held.add(path.stem)
    return held


def thread_metadata(codex_home, held):
    databases = sorted(codex_home.glob('state_*.sqlite'),
                       key=lambda path: int(path.stem.split('_')[-1]))
    if not databases:
        raise RuntimeError('Codex session metadata database is unavailable')
    with closing(sqlite3.connect(databases[-1].resolve().as_uri() + '?mode=ro',
                                uri=True, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ','.join('?' for _ in held)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
        name = 'name' if 'name' in columns else 'NULL AS name'
        rows = connection.execute(
            f'SELECT id, source, {name}, model, reasoning_effort, sandbox_policy, approval_mode '
            f'FROM threads WHERE id IN ({placeholders})', sorted(held)).fetchall()
    return {row['id']: dict(row) for row in rows}


def select_conversation(held, title='', metadata=None):
    hint = TITLE_ID.fullmatch(title)
    if hint:
        candidates = {thread for thread in held if thread.startswith(hint[1])}
        tracking = 'live_title'
    elif metadata is not None:
        candidates = {thread for thread in held
                      if thread not in metadata or metadata[thread]['source'] == 'cli'}
        tracking = 'interactive_metadata'
    else:
        candidates = held
        tracking = 'single_writer'
    thread = next(iter(candidates)) if len(candidates) == 1 else None
    return {'thread_id': thread, 'tracking': tracking if thread else 'unresolved'}


def daemon_conversation(process, title, home, proc):
    if not title:
        return None
    sockets = {os.readlink(fd)[8:-1] for fd in (process / 'fd').iterdir()
               if os.readlink(fd).startswith('socket:[')}
    result = subprocess.run(['ss', '-xnpH'], capture_output=True, text=True, timeout=2, check=True)
    peers = set()
    for row in result.stdout.splitlines():
        fields = row.split()
        if len(fields) >= 9 and fields[7] in sockets and '/codex-daemon-' in fields[4]:
            peers.update(re.findall(r'pid=(\d+)', row))
    held = set()
    for pid in peers:
        peer = proc / pid
        if (peer / 'comm').read_text().strip() == 'codex':
            held.update(writer_thread_ids(peer, home))
    if not held:
        return None
    metadata = thread_metadata(home, held)
    matches = {thread for thread, row in metadata.items() if row.get('name') and
               (title == row['name'] or title.startswith(row['name'] + ' | '))}
    return next(iter(matches)) if len(matches) == 1 else None


def process_identity(pid, proc=Path('/proc'), title='', codex_home=None):
    try:
        stat = (proc / str(pid) / 'stat').read_text()
        started = stat.rsplit(')', 1)[1].split()[19]
    except OSError:
        return {'process': None, 'thread_id': None, 'tracking': 'unresolved'}
    result = {'process': f'{pid}:{started}', 'thread_id': None, 'tracking': 'unresolved'}
    for process in process_tree(pid, proc):
        try:
            if (process / 'comm').read_text().strip() != 'codex':
                continue
            held = writer_thread_ids(process, codex_home)
        except OSError:
            continue
        if not held:
            home = codex_home or Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
            try:
                thread = daemon_conversation(process, title, home, proc)
                if thread:
                    return {**result, 'thread_id': thread, 'tracking': 'daemon_title'}
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
                pass
            continue
        metadata = None
        if len(held) > 1 and not TITLE_ID.fullmatch(title):
            home = codex_home or Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
            try:
                metadata = thread_metadata(home, held)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                pass
        return {**result, **select_conversation(held, title, metadata)}
    return result
