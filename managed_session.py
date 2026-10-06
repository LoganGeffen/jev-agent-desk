import argparse
import json
import os
import re
from pathlib import Path
import secrets
import shlex
import sys
import uuid

import claude_runtime
from codex_actions import tmux
from managed_provider import INSTRUCTIONS, record_path
from pane_identity import CODEX_IDENTITY_CONFIG, UUID
from visual_workspace import process_stamp, visible


ROOT = Path(__file__).resolve().parent


def provider_command(provider, run_dir, identifier, socket, resume=None, model=None, prompt=None):
    bridge = [sys.executable, str(ROOT / 'desk_mcp.py'), '--run-dir', str(run_dir), '--session-id', identifier]
    if provider == 'claude':
        hook = shlex.join([sys.executable, str(ROOT / 'claude_runtime.py'), 'hook', '--socket', socket, '--run-dir', str(run_dir)])
        settings = {'hooks': {event: [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 5}]}] for event in claude_runtime.EVENTS},
                    'permissions': {'allow': ['mcp__desk__*']}}
        argv = ['claude', '--setting-sources', '', '--settings', json.dumps(settings), '--strict-mcp-config', '--mcp-config',
                json.dumps({'mcpServers': {'desk': {'type': 'stdio', 'command': bridge[0], 'args': bridge[1:]}}}),
                '--append-system-prompt', INSTRUCTIONS, '--system-prompt-snapshot', 'off', '--permission-mode', 'acceptEdits']
        if resume:
            argv += ['--resume', resume]
    elif provider == 'codex':
        argv = ['codex', '--no-daemon', '--no-alt-screen', '--sandbox', 'workspace-write', '--ask-for-approval', 'never',
                *CODEX_IDENTITY_CONFIG, '-c', 'developer_instructions=' + json.dumps(INSTRUCTIONS),
                '-c', 'mcp_servers.desk.command=' + json.dumps(bridge[0]),
                '-c', 'mcp_servers.desk.args=' + json.dumps(bridge[1:]),
                '-c', 'mcp_servers.desk.required=true', '-c', 'mcp_servers.desk.default_tools_approval_mode="approve"',
                '-c', 'mcp_servers.desk.tool_timeout_sec=50']
        if resume:
            argv += ['resume', resume]
    else:
        raise ValueError('Managed provider must be claude or codex')
    if model:
        argv += ['--model', model]
    if prompt:
        argv.append(prompt)
    return argv


def launch(socket, source_session, run_dir, provider, label, workspace, resume=None, model=None, prompt=None, account_profile='desk'):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', account_profile) or account_profile.startswith('app-'):
        raise ValueError('Choose a bounded account profile name outside app storage')
    run_dir = Path(run_dir).resolve()
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError('Managed workspace must already exist')
    if not (run_dir / 'url').exists():
        raise ValueError('Start Desk before launching a managed session')
    visible(label, 'session label')
    if resume and not UUID.fullmatch(resume):
        raise ValueError('Resume needs the exact existing conversation UUID')
    if resume:
        from pane_identity import process_identity
        rows = tmux(socket, 'list-panes', '-a', '-F', '#{pane_pid}\t#{pane_current_command}\t#{pane_title}').splitlines()
        for row in rows:
            pid, command, title = row.split('\t', 2)
            identity = process_identity(pid, title=title)
            binding = claude_runtime.read_binding(run_dir, identity['process']) if command == 'claude' else None
            thread = binding['session_id'] if binding else identity.get('thread_id')
            if thread == resume:
                raise ValueError('Stop the original conversation before managed resume; live adoption is unsupported')
    identifier = uuid.uuid4().hex
    argv = [sys.executable, str(ROOT / 'managed_session.py'), '--worker', '--socket', socket,
            '--source-session', source_session, '--run-dir', str(run_dir), '--provider', provider,
            '--workspace', str(workspace), '--session-id', identifier, '--account-profile', account_profile]
    if resume:
        argv += ['--resume', resume]
    if model:
        argv += ['--model', model]
    if prompt:
        argv += ['--prompt', prompt]
    window, pane = tmux(socket, 'new-window', '-d', '-P', '-F', '#{window_id}\t#{pane_id}', '-t', source_session + ':',
                        '-n', label, '-c', str(workspace), 'exec ' + shlex.join(argv)).split('\t')
    return {'session_id': identifier, 'tab': window, 'pane': pane, 'provider': provider, 'workspace': str(workspace),
            'resume': resume, 'adoption': 'managed resume' if resume else 'new managed session'}


def worker(args):
    pane = os.environ['TMUX_PANE']
    tab = tmux(args.socket, 'display-message', '-p', '-t', pane, '#{window_id}')
    pid = int(tmux(args.socket, 'display-message', '-p', '-t', pane, '#{pane_pid}'))
    if pid != os.getpid():
        raise ValueError('Managed launcher must own the exact pane process')
    path = record_path(args.run_dir, args.session_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {'token': secrets.token_urlsafe(32), 'pid': pid, 'process': process_stamp(pid), 'tab': tab, 'pane': pane,
              'provider': args.provider, 'workspace': str(Path(args.workspace).resolve()), 'profile_id': args.account_profile, 'resume': args.resume}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(record, stream)
    environment = {key: value for key, value in os.environ.items() if key not in {'TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID', 'CLAUDECODE', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY'}}
    if args.provider == 'claude':
        environment.update(ENABLE_TOOL_SEARCH='false', MCP_CONNECTION_NONBLOCKING='0')
    argv = provider_command(args.provider, args.run_dir, args.session_id, args.socket, args.resume, args.model, args.prompt)
    os.execvpe(argv[0], argv, environment)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--socket', required=True)
    parser.add_argument('--source-session', default='tabs')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--provider', choices=['claude', 'codex'], required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--session-id')
    parser.add_argument('--label')
    parser.add_argument('--resume')
    parser.add_argument('--model')
    parser.add_argument('--prompt')
    parser.add_argument('--account-profile', default='desk')
    args = parser.parse_args()
    if args.worker:
        worker(args)
    else:
        print(json.dumps(launch(args.socket, args.source_session, args.run_dir, args.provider, args.label or args.provider.title(), args.workspace, args.resume, args.model, args.prompt, args.account_profile)))
