import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sys

from codex_actions import tmux
from pane_identity import process_identity, UUID


EVENTS = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'Stop', 'StopFailure')


def binding_path(run_dir, process):
    if not re.fullmatch(r'\d+:\d+', process or ''):
        raise ValueError('Invalid Claude process binding')
    return Path(run_dir) / 'claude-bindings' / (process + '.json')


def read_binding(run_dir, process):
    try:
        binding = json.loads(binding_path(run_dir, process).read_text())
    except (OSError, ValueError):
        return None
    if binding.get('process') != process or not UUID.fullmatch(binding.get('session_id', '')):
        return None
    return binding if binding.get('active') else None


def command(socket, run_dir, resume=None, fork=False):
    hook = shlex.join([sys.executable, str(Path(__file__).resolve()), 'hook',
                       '--socket', socket, '--run-dir', str(Path(run_dir).resolve())])
    settings = {'hooks': {event: [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 5}]}]
                          for event in EVENTS}}
    args = ['env', '-u', 'TYPESAFE_API_KEY', '-u', 'ELEVENLABS_API_KEY', '-u', 'CLAUDECODE',
            'claude', '--restricted', '--tools', '', '--permission-mode', 'dontAsk',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
            '--settings', json.dumps(settings), '--model', 'haiku',
            '--system-prompt', 'You are a disposable Jev sandbox test session. Answer briefly. Do not use tools.']
    if resume:
        args.extend(['--resume', resume])
    if fork:
        if not resume:
            raise ValueError('Fork requires a resume session')
        args.append('--fork-session')
    return 'exec ' + shlex.join(args)


def apply_event(previous, event, process):
    kind, session = event.get('hook_event_name'), event.get('session_id', '')
    if kind not in EVENTS or event.get('agent_id') or not UUID.fullmatch(session):
        return previous
    if kind != 'SessionStart' and previous and previous.get('session_id') != session:
        return previous
    record = dict(previous or {}) if kind != 'SessionStart' and previous and previous.get('session_id') == session else {}
    record.update(process=process, session_id=session, transcript_path=event.get('transcript_path'),
                  active=kind != 'SessionEnd', event=kind, observed_at=datetime.now(timezone.utc).isoformat())
    if kind == 'SessionStart':
        record.update(state='idle', source=event.get('source'))
    elif kind == 'UserPromptSubmit':
        record.update(state='working', last_prompt=event.get('prompt', ''))
    elif kind == 'Stop':
        record.update(state='ready', last_reply=event.get('last_assistant_message', ''))
    elif kind == 'StopFailure':
        record.update(state='needs_you')
    return record


def hook(socket, run_dir, event):
    pane = os.environ.get('TMUX_PANE', '')
    if not re.fullmatch(r'%\d+', pane):
        return
    pid = tmux(socket, 'display-message', '-p', '-t', pane, '#{pane_pid}')
    ancestor = os.getpid()
    while ancestor > 1 and str(ancestor) != pid:
        stat = (Path('/proc') / str(ancestor) / 'stat').read_text().rsplit(')', 1)[1].split()
        ancestor = int(stat[1])
    if str(ancestor) != pid:
        return
    process = process_identity(pid)['process']
    path = binding_path(run_dir, process)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(path.read_text()) if path.exists() else None
        record = apply_event(previous, event, process)
        if record is not None:
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record) + '\n')
            temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['hook'])
    parser.add_argument('--socket', required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    hook(args.socket, args.run_dir, json.load(sys.stdin))
