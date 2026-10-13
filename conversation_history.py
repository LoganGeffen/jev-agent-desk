import json
from pathlib import Path

import claude_actions
import codex_actions


def display_turns(turns):
    output = []
    for turn in turns:
        items = []
        for item in turn.get('items', []):
            if item['type'] == 'reasoning':
                continue
            if item['type'] in ('userMessage', 'agentMessage'):
                items.append(item)
            else:
                preview = {key: value for key, value in item.items() if key not in ('id', 'type', 'tool', 'status')}
                text = json.dumps(preview, ensure_ascii=False)
                items.append({**{key: item[key] for key in ('id', 'type', 'tool', 'status') if key in item},
                              'preview': text[:2000], 'shortened': len(text) > 2000})
        output.append({'id': turn['id'], 'status': turn.get('status'), 'items': items})
    return output


def reverse_lines(stream, end=None):
    position = stream.seek(0, 2) if end is None else end
    pending = b''
    while position:
        size = min(position, 65536)
        position -= size
        stream.seek(position)
        data = stream.read(size) + pending
        parts = data.split(b'\n')
        pending = parts[0]
        offset = position + len(data)
        for line in reversed(parts[1:]):
            offset -= len(line)
            yield offset, line
            offset -= 1
    if pending:
        yield 0, pending


def claude_page(binding, before, limit):
    session = binding['session_id']
    path = Path(binding['transcript_path']).resolve()
    root = (Path.home() / '.claude/projects').resolve()
    if path.parent.parent != root or path.name != session + '.jsonl':
        raise ValueError('Claude transcript is outside its expected session location')
    end, cursor = (None, None) if before is None else before.split(':', 1)
    if end is not None:
        end = int(end)
        if end < 0 or end > path.stat().st_size:
            raise ValueError('Invalid history position')
    chain, count, offset = [], 0, 0
    with path.open('rb') as stream:
        for offset, line in reverse_lines(stream, end):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if not chain:
                    continue
                raise
            if not row.get('uuid') or row.get('isSidechain'):
                continue
            if cursor and row['uuid'] != cursor:
                continue
            chain.append(row)
            cursor = row.get('parentUuid')
            if row.get('type') == 'user':
                content = row.get('message', {}).get('content', [])
                text = content if isinstance(content, str) else '\n'.join(
                    part.get('text', '') for part in content if part.get('type') == 'text')
                if text and not text.startswith(('<local-command-', '<command-', '<task-notification>', '[Request interrupted')):
                    count += 1
            if count >= limit or not cursor:
                break
    if before and not chain:
        raise ValueError('History position is no longer available')
    return {'turns': claude_actions.parse_turns(chain), 'nextCursor': f'{offset}:{cursor}' if cursor else None}


def read_page(provider, thread_id, binding=None, before=None):
    if provider == 'codex':
        return codex_actions.thread_read(thread_id, page_size=20, before=before)
    if provider == 'claude' and binding and binding['session_id'] == thread_id:
        return claude_page(binding, before, 20)
    raise ValueError('Conversation history is not available for this pane')
