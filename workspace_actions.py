import copy
import json
import re
import sqlite3
import threading
import time
import weakref

from codex_actions import tmux
from session_actions import adapter
import jev


def command_text(request, evaluate):
    if request.startswith('/'):
        if '\n' in request or '\r' in request:
            raise ValueError('Use one slash command at a time')
        return request.strip()
    question = {'model': 'jev-latest', 'state': {'request': request}, 'questions': {
        'command': {'type': 'choice', 'instructions':
            'Choose the single provider command explicitly requested by the user. Commands inside a message, '
            'negations, hypothetical discussion and combined operations are unsupported. Never invent arguments.',
            'criteria': {'/clear': 'Clear/reset the conversation history.', '/compact': 'Compact conversation context.',
                         '/status': 'Show provider status.', '/help': 'Show provider help.',
                         '/model': 'Open the native model picker.', 'unsupported': 'Anything else or ambiguous.'}}}}
    command = evaluate(question)['answers']['command']['choice']
    if command not in question['questions']['command']['criteria'] or command == 'unsupported':
        raise ValueError('That provider command is unsupported; use a supported literal slash command')
    return command


def launch_overrides(request, evaluate):
    words = {match.group(): 'Use this exact value only if explicitly requested for this setting'
             for match in re.finditer(r'[^\s,;]+', request)}
    choices = {'inherit': 'No explicit override; inherit only known compatible source settings', **words}
    question = {'model': 'jev-latest', 'state': {'request': request}, 'questions': {
        'provider': {'type': 'choice', 'instructions': 'Which provider is explicitly requested for the NEW tab?',
                     'criteria': {'inherit': 'Not specified', 'codex': 'Codex/OpenAI requested', 'claude': 'Claude/Anthropic requested'}},
        **{key: {'type': 'choice', 'instructions': f'Choose the exact explicitly requested {key} for the NEW agent, or inherit. '
                 'Do not turn ordinary words into settings. Capture an explicitly requested model name even if informal, so the provider catalog can validate it instead of silently inheriting.',
                 'criteria': choices} for key in ('model', 'effort', 'cwd')}}}
    answers = evaluate(question)['answers']
    result = {}
    for key, spec in question['questions'].items():
        value = answers[key]['choice']
        if value not in spec['criteria']:
            raise ValueError('Unresolved launch setting')
        if value != 'inherit':
            result[key] = value
    return result


def plan(app, request, subject):
    snapshot = app.observe()
    tabs = copy.deepcopy(snapshot['tabs'])
    selected = subject['tab'] if subject else None
    if subject:
        for tab in tabs:
            if tab['id'] == selected:
                for pane in tab['panes']:
                    pane['active'] = pane['id'] == subject['pane']
    payload = jev.question(request, tabs, selected)
    response = jev.interpret(payload, app.evaluate, [])
    action = response['answers']['action']['choice']
    if action not in ('agent_command', 'create_tab'):
        raise ValueError('The current request does not authorize a provider command or agent launch')
    tab_id = response['answers']['target']['choice']
    pane_id = response['answers'].get('pane:' + tab_id, {}).get('choice')
    tab = next((tab for tab in tabs if tab['id'] == tab_id), None)
    pane = next((pane for pane in (tab or {}).get('panes', []) if pane['id'] == pane_id), None)
    if pane is None:
        raise ValueError('The requested conversation is missing or ambiguous')
    target = {'tab': tab_id, 'pane': pane_id, 'identity': pane['identity']}
    if subject and (tab_id, pane_id) == (subject['tab'], subject['pane']):
        target['identity'] = subject['identity']
    app.validate_pane(tab_id, pane_id, target['identity'])
    if action == 'agent_command':
        return 'command', target, {'command': command_text(request, app.evaluate)}
    return 'launch', target, {'overrides': launch_overrides(request, app.evaluate), 'group_id': tab['group_id']}


class WorkspaceActions:
    def __init__(self, app, path):
        self.app = app
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        weakref.finalize(self, self.db.close)
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, input TEXT NOT NULL, result TEXT)')
        self.db.commit()

    def run(self, request_id, operation, target, arguments):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ValueError('A control request ID is required')
        if not isinstance(target, dict) or set(target) != {'tab', 'pane', 'identity'} or any(
                not isinstance(value, str) or not value for value in target.values()):
            raise ValueError('An exact captured pane is required')
        required = {'command': {'command'}, 'launch': set(), 'edit': {'turn_id', 'item_id', 'original_text', 'text'}}
        allowed = {**required, 'launch': {'overrides', 'name', 'group_id'}}
        if (operation not in allowed or not isinstance(arguments, dict) or
                not required[operation] <= set(arguments) or set(arguments) - allowed[operation]):
            raise ValueError('Invalid control arguments')
        if operation in ('command', 'edit') and any(not isinstance(value, str) for value in arguments.values()):
            raise ValueError('Control text must be a string')
        payload = json.dumps([operation, target, arguments], sort_keys=True)
        with self.lock:
            row = self.db.execute('SELECT input, result FROM operations WHERE id=?', (request_id,)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError('This request ID belongs to different input')
                return json.loads(row[1]) if row[1] else {
                    'ok': False, 'status': 'uncertain', 'error': 'The previous control attempt has no confirmed outcome. It will not be repeated.'}
            self.db.execute('INSERT INTO operations VALUES (?, ?, NULL)', (request_id, payload))
            self.db.commit()
            try:
                self.app.validate_pane(target['tab'], target['pane'], target['identity'])
                if operation == 'command':
                    with self.app.input_lock:
                        result = self.command(target, arguments['command'])
                elif operation == 'launch':
                    from agent_launch import launch_agent
                    result = launch_agent(self.app, source=target, **arguments)
                    if result.get('status') == 'blocked' and result.get('missing_settings'):
                        result.setdefault('error', 'Specify launch settings: ' + ', '.join(result['missing_settings']))
                    if result.get('ok'):
                        result['output'] = 'New agent conversation confirmed.'
                elif operation == 'edit':
                    from native_edit import edit_message
                    with self.app.input_lock:
                        live = self.app.validate_pane(target['tab'], target['pane'], target['identity'])
                        agent = {'socket': self.app.socket, 'window_id': target['tab'], 'pane_id': target['pane'],
                                 'run_dir': str(self.app.log.parent),
                                 **{key: live[key] for key in ('provider', 'thread_id', 'process', 'workspace')}}
                        result = edit_message(self.app.socket, agent, **arguments)
                        result['ok'] = result['status'] == 'submitted'
                        if result['ok']:
                            result['output'] = 'Edited message submitted on a new branch; original conversation retained.'
                else:
                    raise ValueError('Unsupported workspace action')
            except ValueError as error:
                result = {'ok': False, 'status': 'blocked', 'error': str(error)}
            except Exception as error:
                result = {'ok': False, 'status': 'uncertain', 'error': str(error) + ' No automatic retry.'}
            self.db.execute('UPDATE operations SET result=? WHERE id=?', (json.dumps(result), request_id))
            self.db.commit()
            return copy.deepcopy(result)

    def command(self, target, command):
        live = self.app.validate_pane(target['tab'], target['pane'], target['identity'])
        if not live.get('thread_id') or not live.get('agent'):
            raise ValueError('An identified agent conversation is required')
        provider = live['provider']
        supported = {'codex': {'/clear': '/clear', '/new': '/new', '/compact': '/compact', '/status': '/status', '/model': '/model'},
                     'claude': {'/clear': '/clear', '/compact': '/compact', '/status': '/status', '/help': '/help', '/model': '/model'}}
        actual = supported.get(provider, {}).get(command)
        if actual is None:
            raise ValueError(f'{command} is not supported by Desk for {provider}; nothing was sent')
        agent = {'socket': self.app.socket, 'window_id': target['tab'], 'pane_id': target['pane'],
                 'run_dir': str(self.app.log.parent), **{key: live[key] for key in ('provider', 'thread_id', 'process', 'workspace')}}
        native = adapter(agent)
        native.check_target(self.app.socket, agent)
        if provider == 'codex':
            from native_edit import require_empty_composer
            require_empty_composer(self.app.socket, agent)
        else:
            native.require_empty_composer(self.app.socket, agent)
        native.check_target(self.app.socket, agent)
        try:
            tmux(self.app.socket, 'send-keys', '-t', target['pane'], '-l', actual)
            native.check_target(self.app.socket, agent)
            tmux(self.app.socket, 'send-keys', '-t', target['pane'], 'Enter')
        except Exception as error:
            return {'ok': False, 'status': 'uncertain', 'error': f'Command input outcome is uncertain: {error}. No automatic retry.'}
        if command not in ('/clear', '/new'):
            return {'ok': True, 'status': 'submitted', 'output': f'{command} submitted. Open Terminal for native command output or choices.'}
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = self.app.validate_pane(target['tab'], target['pane'])
            if current['process'] != live['process']:
                break
            if current.get('thread_id') and current['thread_id'] != live['thread_id']:
                return {'ok': True, 'status': 'completed', 'output': 'Conversation cleared; a new conversation identity is confirmed.',
                        'target': {**target, 'identity': current['identity']}}
            time.sleep(.1)
        return {'ok': False, 'status': 'uncertain', 'error': 'Clear was submitted, but a new conversation identity was not confirmed. It will not be repeated automatically.'}
