import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time

from claude_runtime import hook_settings
from codex_actions import tmux
from pane_identity import CODEX_IDENTITY_CONFIG, thread_metadata


FIELDS = ('provider', 'model', 'effort', 'cwd')


def source_settings(pane):
    settings = {'provider': pane.get('provider'), 'cwd': pane.get('workspace')}
    if pane.get('provider') == 'codex' and pane.get('thread_id'):
        home = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
        try:
            metadata = thread_metadata(home, {pane['thread_id']}).get(pane['thread_id'], {})
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            metadata = {}
        settings.update(model=metadata.get('model'), effort=metadata.get('reasoning_effort'))
    return settings


def validate_source(playground, source):
    if not isinstance(source, dict) or set(source) != {'tab', 'pane', 'identity'}:
        raise ValueError('An exact source pane is required')
    if not all(isinstance(value, str) and value for value in source.values()):
        raise ValueError('An exact source pane is required')
    return playground.validate_pane(source['tab'], source['pane'], source['identity'])


def resolve_settings(playground, source, overrides=None):
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - set(FIELDS):
        raise ValueError('Launch settings are provider, model, effort and cwd')
    for key, value in overrides.items():
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError(f'Launch {key} must be nonempty text')
    pane = validate_source(playground, source)
    inherited = source_settings(pane)
    provider = overrides.get('provider', inherited.get('provider'))
    if provider is not None and provider not in ('codex', 'claude'):
        raise ValueError('Choose Codex or Claude')
    if provider != inherited.get('provider'):
        inherited.pop('model', None)
        inherited.pop('effort', None)
    settings = {key: overrides.get(key, inherited.get(key)) for key in FIELDS}
    if settings['cwd'] and (not Path(settings['cwd']).is_absolute() or not Path(settings['cwd']).is_dir()):
        raise ValueError('Launch cwd must be an existing absolute directory')
    missing = [key for key in FIELDS if not settings[key]]
    return {'status': 'missing_settings' if missing else 'ready', 'settings': settings,
            'missing': missing, 'source': dict(source)}


def provider_command(settings, socket, run_dir):
    if settings['provider'] == 'codex':
        argv = ['codex', '--no-daemon', '--no-alt-screen', *CODEX_IDENTITY_CONFIG,
                '--model', settings['model'], '-c', 'model_reasoning_effort=' + json.dumps(settings['effort'])]
    elif settings['provider'] == 'claude':
        argv = ['claude', '--settings', json.dumps(hook_settings(socket, run_dir)),
                '--model', settings['model'], '--effort', settings['effort']]
    else:
        raise ValueError('Choose Codex or Claude')
    return ['env', '-u', 'TYPESAFE_API_KEY', '-u', 'ELEVENLABS_API_KEY',
            '-u', 'ELEVENLABS_VOICE_ID', '-u', 'CLAUDECODE', *argv]


def launch_agent(playground, source, overrides=None, name=None, group_id=None, timeout=15):
    with playground.input_lock:
        result = resolve_settings(playground, source, overrides)
        if result['missing']:
            return {**result, 'ok': False, 'status': 'blocked', 'missing_settings': result['missing'],
                    'attempted': False, 'retryable': False}
        settings = result['settings']
        name = name or settings['provider'].title()
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('Choose a tab name of 1 to 80 visible characters')
        before = playground.observe()
        known_threads = {pane.get('thread_id') for tab in before['tabs'] for pane in tab['panes']}
        if group_id is not None and group_id not in {group['id'] for group in before['groups']}:
            raise ValueError('That tab group is no longer available')
        argv = provider_command(settings, playground.socket, playground.log.parent)
        validate_source(playground, source)
        result.update(ok=False, status='uncertain', attempted=True, retryable=False)
        try:
            created = tmux(playground.socket, 'new-window', '-d', '-P', '-F', '#{window_id}\t#{pane_id}',
                           '-t', playground.source_session + ':', '-n', name, '-c', settings['cwd'],
                           'exec ' + shlex.join(argv))
            tab, pane_id = created.split('\t')
            result.update(tab=tab, pane=pane_id)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            result['error'] = f'Launch outcome could not be confirmed: {error}'
            return result
    deadline = time.monotonic() + timeout
    while True:
        try:
            current = playground.observe()
            tab_state = next((item for item in current['tabs'] if item['id'] == tab), None)
            pane = next((item for item in (tab_state or {}).get('panes', []) if item['id'] == pane_id), None)
            if pane is None:
                result.update(status='failed', error='The new agent exited before its conversation was observed')
                return result
            if pane.get('agent') and pane.get('provider') == settings['provider'] and pane.get('thread_id'):
                if pane['thread_id'] in known_threads:
                    result['error'] = 'The new pane resolved to an existing conversation; launch is unconfirmed'
                    return result
                playground.validate_pane(tab, pane_id, pane['identity'])
                result.update(ok=True, status='completed', identity=pane['identity'], thread_id=pane['thread_id'])
                if group_id is not None:
                    playground.groups.move(tab, group_id, None, current['tabs'])
                playground.focus(tab, pane_id)
                return result
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            result.update(ok=False, status='uncertain', error=f'Launch observation failed: {error}')
            return result
        if time.monotonic() >= deadline:
            result['error'] = 'The new tab exists, but its conversation identity is not yet confirmed'
            return result
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
