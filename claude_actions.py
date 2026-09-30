import json
from pathlib import Path
import re
import time

from claude_runtime import read_binding
from codex_actions import focus, latest_reply, tmux
from pane_identity import process_identity


def check_target(socket, agent):
    actual = tmux(socket, 'display-message', '-p', '-t', agent['pane_id'],
                  '#{window_id}\t#{pane_current_command}\t#{pane_pid}')
    window, command, pid = actual.split('\t')
    if socket != agent['socket'] or window != agent['window_id'] or command != 'claude':
        raise RuntimeError('The Claude pane is no longer running at this target')
    process = process_identity(pid)['process']
    binding = read_binding(agent['run_dir'], process)
    if process != agent['process'] or not binding or binding['session_id'] != agent['thread_id']:
        raise RuntimeError('The Claude pane or conversation changed; try again')
    return binding


def thread_read(binding):
    session = binding['session_id']
    path = Path(binding['transcript_path']).resolve()
    root = (Path.home() / '.claude/projects').resolve()
    if not path.is_relative_to(root) or path.name != session + '.jsonl' or path.parent.parent != root:
        raise ValueError('Claude transcript is outside its expected session location')
    try:
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - 2_000_000)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return {'id': session, 'turns': [], 'history_missing': True}
    nodes = {}
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        if row.get('uuid') and not row.get('isSidechain'):
            nodes[row['uuid']] = row
    chain, seen = [], set()
    cursor = next(reversed(nodes), None)
    while cursor in nodes and cursor not in seen:
        seen.add(cursor)
        row = nodes[cursor]
        chain.append(row)
        cursor = row.get('parentUuid')
    turns = []
    for row in reversed(chain):
        kind = row.get('type')
        content = row.get('message', {}).get('content', [])
        content = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
        text = '\n'.join(part.get('text', '') for part in content if part.get('type') == 'text')
        if kind == 'user' and text and not text.startswith(('<local-command-', '<command-', '<task-notification>')):
            if text.startswith('[Request interrupted by user'):
                if turns:
                    turns[-1]['status'] = 'interrupted'
                continue
            turns.append({'id': row['uuid'], 'status': 'inProgress', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}]})
        elif kind == 'assistant' and turns:
            final = row.get('message', {}).get('stop_reason') == 'end_turn'
            if text:
                turns[-1]['items'].append({'type': 'agentMessage', 'text': text,
                                          'phase': 'final_answer' if final else 'commentary'})
            if final:
                turns[-1].update(status='completed', completedAt=row.get('timestamp'))
            for part in content:
                if part.get('type') == 'tool_use':
                    turns[-1]['items'].append({'type': 'mcpToolCall', 'tool': part.get('name'),
                                               'status': 'started', 'command': part.get('input')})
        elif kind == 'user' and turns:
            for part in content:
                if part.get('type') == 'tool_result':
                    turns[-1]['items'].append({'type': 'mcpToolCall', 'tool': part.get('tool_use_id'),
                                               'status': 'failed' if part.get('is_error') else 'completed',
                                               'result': part.get('content')})
    return {'id': session, 'turns': turns, 'history_truncated': bool(start or cursor)}


def working_indicator(screen):
    return bool(re.search(r'(?im)^.*\besc to interrupt\b.*$', '\n'.join(screen.splitlines()[-8:])))


def send_message(socket, agent, text):
    check_target(socket, agent)
    if not agent.get('preserve_focus'):
        focus(socket, agent['window_id'], agent['pane_id'], agent.get('view_session', 'tabs'))
    tmux(socket, 'load-buffer', '-b', 'jev-claude-message', '-', input=text)
    tmux(socket, 'paste-buffer', '-b', 'jev-claude-message', '-d', '-p', '-t', agent['pane_id'])
    time.sleep(.2)
    check_target(socket, agent)
    tmux(socket, 'send-keys', '-t', agent['pane_id'], 'Enter')


def read_reply(socket, agent):
    binding = check_target(socket, agent)
    if not agent.get('preserve_focus'):
        focus(socket, agent['window_id'], agent['pane_id'], agent.get('view_session', 'tabs'))
    reply = binding.get('last_reply')
    if not reply:
        history = thread_read(binding)
        reply = ('Claude has not persisted this conversation yet; no reply is available to read.'
                 if history.get('history_missing') else latest_reply(history))
    check_target(socket, agent)
    return reply


def interrupt_turn(socket, agent):
    check_target(socket, agent)
    pane = agent['pane_id']
    if not agent.get('preserve_focus'):
        focus(socket, agent['window_id'], pane, agent.get('view_session', 'tabs'))
    if not working_indicator(tmux(socket, 'capture-pane', '-p', '-t', pane)):
        return 'No running turn is shown; nothing was interrupted.'
    tmux(socket, 'send-keys', '-t', pane, 'Escape')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(.1)
        check_target(socket, agent)
        if not working_indicator(tmux(socket, 'capture-pane', '-p', '-t', pane)):
            return 'Interrupted the current turn. The session remains open.'
    raise TimeoutError('Interrupt sent, but Claude still shows a running turn')
