from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

import claude_actions
from claude_runtime import read_binding
from pane_identity import UUID, process_tree, writer_thread_ids


READ_LIMIT = 16 * 1024 * 1024


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is not None:
            return result
    raise ValueError('Missing lifecycle timestamp')


def process_started(process):
    pid, ticks = process.split(':')
    stat = (Path('/proc') / pid / 'stat').read_text().rsplit(')', 1)[1].split()
    if stat[19] != ticks:
        raise ValueError('Process generation changed')
    boot = next(line.split()[1] for line in Path('/proc/stat').read_text().splitlines()
                if line.startswith('btime '))
    return timestamp(int(boot) + int(ticks) / os.sysconf('SC_CLK_TCK'))


def codex_process_started(agent, codex_home):
    writers = []
    for process in process_tree(agent['process'].split(':')[0]):
        try:
            if (process / 'comm').read_text().strip() != 'codex':
                continue
            if agent['thread_id'] in writer_thread_ids(process, codex_home):
                ticks = (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                writers.append(process_started(f'{process.name}:{ticks}'))
        except OSError:
            continue
    if len(writers) > 1:
        raise ValueError('Ambiguous conversation writer')
    return writers[0] if writers else process_started(agent['process'])


def observation(state='unknown', turn_id=None, observed_at=None, source=None, reason=None):
    result = {'state': state, 'turn_id': turn_id, 'observed_at': observed_at, 'source': source}
    if reason:
        result['reason'] = reason
    return result


class AgentActivity:
    def __init__(self, codex_home=None):
        self.codex_home = Path(codex_home or os.environ.get('CODEX_HOME', '~/.codex')).expanduser().resolve()
        self.cache = {}
        self.lock = threading.Lock()

    def read(self, agent, thread=None):
        source = 'claude_hooks' if agent.get('provider') == 'claude' else 'codex_rollout'
        try:
            with self.lock:
                if agent.get('provider') == 'claude':
                    return self.claude(agent, thread)
                if agent.get('provider') == 'codex':
                    return self.codex(agent)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
                StopIteration, sqlite3.Error, RuntimeError):
            return observation(source=source, reason='Lifecycle evidence unavailable')
        return observation(reason='Provider activity unavailable')

    def rollout_path(self, thread_id):
        if not UUID.fullmatch(thread_id or ''):
            raise ValueError('Invalid conversation identity')
        databases = [path for path in self.codex_home.glob('state_*.sqlite')
                     if path.stem.removeprefix('state_').isdigit()]
        if not databases:
            raise ValueError('Codex metadata unavailable')
        database = max(databases, key=lambda path: int(path.stem.removeprefix('state_')))
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            row = connection.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
        if not row:
            raise ValueError('Conversation rollout unavailable')
        path = Path(row[0]).resolve()
        if not path.is_relative_to(self.codex_home) or thread_id not in path.name or path.suffix != '.jsonl':
            raise ValueError('Unexpected conversation rollout path')
        return path

    def codex(self, agent):
        path = self.rollout_path(agent['thread_id'])
        key = (agent['thread_id'], str(path))
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            cached = self.cache.get(key)
            reset = (not cached or cached['file'] != identity or stat.st_size < cached['size']
                     or (stat.st_size == cached['size'] and stat.st_mtime_ns != cached['mtime']))
            if reset or stat.st_size - cached['offset'] > READ_LIMIT:
                generation = cached['generation'] + 1 if cached else 0
                cached = {'file': identity, 'offset': max(0, stat.st_size - READ_LIMIT),
                          'value': observation(source='codex_rollout', reason='No lifecycle marker in available history'),
                          'started_turn': None, 'generation': generation}
                stream.seek(cached['offset'])
                if cached['offset']:
                    stream.readline(READ_LIMIT)
                    cached['offset'] = stream.tell()
            else:
                stream.seek(cached['offset'])
            data = stream.read(READ_LIMIT)
        complete = data.rfind(b'\n') + 1
        for line in data[:complete].splitlines():
            try:
                row = json.loads(line)
                self.reduce(cached, row)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                cached['value'] = observation(source='codex_rollout', reason='Malformed lifecycle history')
        cached.update(offset=cached['offset'] + complete, size=stat.st_size, mtime=stat.st_mtime_ns)
        self.cache[key] = cached
        if len(self.cache) > 128:
            self.cache.pop(next(iter(self.cache)))
        generation = f'{identity[0]}:{identity[1]}:{cached["generation"]}'
        if complete < len(data):
            return {**observation(source='codex_rollout', reason='Lifecycle history is being written'),
                    'generation': generation}
        value = {**cached['value'], 'generation': generation}
        if value['state'] == 'working':
            if timestamp(value['observed_at']) < codex_process_started(agent, self.codex_home):
                return {**observation(source='codex_rollout', reason='Unclosed turn predates current process'),
                        'generation': generation}
        return value

    @staticmethod
    def reduce(cached, row):
        if not isinstance(row, dict):
            raise ValueError('Invalid history record')
        if row.get('type') != 'event_msg':
            return
        payload = row['payload']
        kind = payload.get('type')
        if kind not in ('task_started', 'task_complete', 'turn_aborted'):
            return
        turn_id = payload.get('turn_id')
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('Missing turn identity')
        event_time = timestamp(row.get('timestamp'))
        previous_time = cached['value'].get('observed_at')
        if previous_time and event_time < timestamp(previous_time):
            return
        if kind == 'task_started':
            cached['started_turn'] = turn_id
            state = 'working'
        else:
            if cached['started_turn'] and turn_id != cached['started_turn']:
                return
            state = ('stopped' if kind == 'turn_aborted' else
                     'ready' if isinstance(payload.get('last_agent_message'), str)
                     and payload['last_agent_message'].strip() else 'idle')
        cached['value'] = observation(state, turn_id, event_time.isoformat(), 'codex_rollout')

    @staticmethod
    def claude(agent, thread):
        binding = read_binding(agent['run_dir'], agent['process'])
        if not binding or binding['session_id'] != agent['thread_id']:
            return observation(source='claude_hooks', reason='Claude lifecycle binding unavailable')
        hook_time = timestamp(binding.get('observed_at'))
        event = binding.get('event')
        if thread is None:
            thread = claude_actions.thread_read(binding)
        if thread.get('id', agent['thread_id']) != agent['thread_id'] or thread.get('history_missing'):
            return observation(source='claude_hooks', reason='Claude transcript unavailable')
        turns = thread['turns']
        latest = turns[-1] if turns else None
        started = timestamp(latest['startedAt']) if latest and latest.get('startedAt') else None
        ended = timestamp(latest['completedAt']) if latest and latest.get('completedAt') else None
        current = bool(started and started <= hook_time)
        if latest and ended and ended >= hook_time and latest['status'] in ('interrupted', 'failed'):
            return observation('stopped' if latest['status'] == 'interrupted' else 'failed',
                               latest['id'], ended.isoformat(), 'claude_transcript')
        if started and started > hook_time:
            state = 'working' if latest['status'] == 'inProgress' else 'unknown'
            return observation(state, latest['id'], started.isoformat(), 'claude_transcript',
                               'Transcript has a newer turn than the lifecycle hook')
        if event == 'UserPromptSubmit':
            turn_id = latest['id'] if current and (not ended or ended >= hook_time) else None
            return observation('working', turn_id, hook_time.isoformat(), 'claude_hooks')
        if event == 'StopFailure':
            return observation('failed', latest['id'] if current else None, hook_time.isoformat(), 'claude_hooks')
        if event == 'SessionStart':
            return observation('idle', observed_at=hook_time.isoformat(), source='claude_hooks')
        if event == 'Stop' and current and ended and ended <= hook_time and latest['status'] == 'completed':
            replies = [item['text'] for item in latest['items'] if item['type'] == 'agentMessage'
                       and item.get('phase') in (None, 'final_answer') and item['text'].strip()]
            if not replies and not binding.get('last_reply'):
                return observation('idle', latest['id'], hook_time.isoformat(), 'claude_hooks')
            if replies and replies[-1] == binding.get('last_reply'):
                return observation('ready', latest['id'], hook_time.isoformat(), 'claude_hooks')
        return observation(source='claude_hooks', reason='Claude lifecycle and latest transcript do not agree')
