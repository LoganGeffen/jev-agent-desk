import re
import subprocess
import time

import codex_actions
from pane_identity import process_identity


ANSI = re.compile(r'\x1b\[[0-9;]*m')


def require_empty_composer(socket, agent):
    codex_actions.check_target(socket, agent)
    screen = codex_actions.tmux(socket, 'capture-pane', '-p', '-e', '-t', agent['pane_id'])
    plain = ANSI.sub('', screen)
    if codex_actions.working_indicator(plain):
        raise ValueError('Wait for the running turn before changing the conversation')
    prompts = [line for line in screen.splitlines() if ANSI.sub('', line).startswith('› ')]
    if not prompts or not re.fullmatch(r'\x1b\[1m›\x1b\[0m \x1b\[2m[^\x1b\n]+\x1b\[0m', prompts[-1]):
        raise ValueError('Empty the native terminal composer before changing the conversation')
    tail = plain.rsplit('› ', 1)[-1].splitlines()[1:]
    if tail and tail[0].strip():
        raise ValueError('The native terminal composer is not empty')


def edit_target(thread, turn_id, item_id, original_text):
    turns = thread['turns']
    if any(turn['status'] == 'inProgress' for turn in turns):
        raise ValueError('Wait for the running turn before editing')
    for index, turn in enumerate(turns):
        if turn['id'] != turn_id:
            continue
        items = [item for item in turn['items'] if item['type'] == 'userMessage']
        if len(items) != 1 or items[0].get('id') != item_id:
            break
        content = items[0].get('content', [])
        if not content or any(part.get('type') != 'text' for part in content):
            raise ValueError('Editing messages with attachments is not supported yet')
        if ''.join(part['text'] for part in content) != original_text:
            break
        return index
    raise ValueError('The selected message changed or is no longer available')


def observed_identity(socket, agent):
    actual = codex_actions.tmux(socket, 'display-message', '-p', '-t', agent['pane_id'],
                                '#{window_id}\t#{pane_current_command}\t#{pane_pid}\t#{pane_title}')
    window, command, pid, title = actual.split('\t', 3)
    if window != agent['window_id'] or command != 'codex':
        raise RuntimeError('The target pane changed during editing')
    identity = process_identity(pid, title=title)
    if identity['process'] != agent['process']:
        raise RuntimeError('The target process changed during editing')
    return identity


def edit_message(socket, agent, turn_id, item_id, original_text, text):
    result = {'status': 'failed', 'source_thread_id': agent.get('thread_id'),
              'branch_thread_id': None, 'session_id': None, 'draft': text}
    try:
        if agent.get('provider', 'codex') != 'codex':
            raise ValueError('Native message editing is currently supported for Codex only')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('The edited message cannot be empty')
        if not agent.get('process'):
            raise ValueError('The target process identity is unavailable')
        require_empty_composer(socket, agent)
        source = codex_actions.thread_read(agent['thread_id'])
        index = edit_target(source, turn_id, item_id, original_text)
        if source.get('historyMode') != 'paginated':
            raise ValueError('Native editing requires paginated Codex history')
        if not source.get('model') or not source.get('reasoningEffort') or not source.get('cwd'):
            raise ValueError('The original model, reasoning effort or directory is unknown')
        prefix = [turn['id'] for turn in source['turns'][:index]]
        require_empty_composer(socket, agent)
        with codex_actions.app_server() as call:
            params = {'threadId': source['id'], 'excludeTurns': True,
                      'model': source['model'], 'cwd': source['cwd'],
                      'config': {'model_reasoning_effort': source['reasoningEffort']}}
            if index:
                params['lastTurnId'] = prefix[-1]
            branch = call('thread/fork', params)['thread']
            result.update(branch_thread_id=branch['id'], session_id=branch.get('sessionId'))
            if branch['id'] == source['id'] or branch.get('forkedFromId') != source['id']:
                raise RuntimeError('Codex did not establish a separate native branch')
            if not index:
                call('thread/revert', {'threadId': branch['id'], 'beforeTurnId': turn_id})
        branch_history = codex_actions.thread_read(branch['id'])
        if [turn['id'] for turn in branch_history['turns']] != prefix:
            raise RuntimeError('The native branch does not match the selected edit point')
        current = codex_actions.thread_read(source['id'])
        if current['turns'] != source['turns']:
            raise RuntimeError('The original conversation changed while preparing the branch')
        require_empty_composer(socket, agent)
        result['status'] = 'uncertain'
        codex_actions.send_message(socket, agent, '/resume ' + branch['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            identity = observed_identity(socket, agent)
            if identity['thread_id'] == branch['id']:
                try:
                    require_empty_composer(socket, {**agent, 'thread_id': branch['id']})
                    break
                except ValueError:
                    time.sleep(.1)
                    continue
            if identity['thread_id'] not in (None, source['id']):
                raise RuntimeError('A different conversation became active during editing')
            time.sleep(.1)
        else:
            raise TimeoutError('Native branch activation was not confirmed; the draft was retained')
        target = {**agent, 'thread_id': branch['id']}
        require_empty_composer(socket, target)
        codex_actions.send_message(socket, target, text)
        result['status'] = 'submitted'
        return result
    except (OSError, RuntimeError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
        return result
