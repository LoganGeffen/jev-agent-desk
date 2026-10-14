import hashlib
import json
from pathlib import Path

import claude_actions
import codex_actions


DETAIL_CHARS = 32000


def visible_item(value):
    if isinstance(value, list):
        return [visible_item(part) for part in value
                if not isinstance(part, dict) or part.get('type') not in ('reasoning', 'thinking', 'redacted_thinking')]
    if not isinstance(value, dict):
        return value
    if value.get('type') in ('image', 'inputImage', 'localImage', 'audio', 'localAudio', 'document', 'file'):
        metadata = {key: value[key] for key in ('type', 'name', 'filename', 'fileName', 'path', 'fileId', 'mimeType', 'media_type') if key in value}
        source = value.get('source', {})
        if isinstance(source, dict) and source.get('media_type'):
            metadata['media_type'] = source['media_type']
        metadata['availability'] = 'Metadata only; open the original attachment in the provider.'
        return metadata
    return {key: visible_item(part) for key, part in value.items()
            if key not in ('reasoning', 'thinking', 'encrypted_content', 'encryptedContent', 'signature')}


def item_text(item):
    return json.dumps(visible_item(item), ensure_ascii=False, indent=2)


def item_key(text):
    return hashlib.sha256(text.encode()).hexdigest()


def display_turns(turns, before=None):
    output = []
    for turn in turns:
        items = []
        for index, item in enumerate(turn.get('items', [])):
            if item['type'] in ('reasoning', 'thinking', 'redacted_thinking'):
                continue
            if item['type'] in ('userMessage', 'agentMessage'):
                items.append(visible_item(item))
            else:
                text = item_text(item)
                items.append({**{key: item[key] for key in ('id', 'type', 'tool', 'status') if key in item},
                              'preview': text[:2000], 'shortened': len(text) > 2000,
                              'detail_ref': {'before': before, 'turn_id': turn['id'],
                                             'item_index': index, 'item_key': item_key(text)}})
        output.append({'id': turn['id'], 'status': turn.get('status'), 'items': items})
    return output


def read_details(provider, thread_id, binding=None, before=None, *, turn_id, item_index, item_key, offset=0):
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 8192:
        raise ValueError('An exact turn is required')
    if type(item_index) is not int or item_index < 0 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid detail position')
    if not isinstance(item_key, str) or len(item_key) != 64:
        raise ValueError('An exact item key is required')
    if before is not None and (not isinstance(before, str) or len(before) > 8192):
        raise ValueError('Invalid history position')
    page = read_page(provider, thread_id, binding, before)
    turn = next((turn for turn in page.get('turns', []) if turn['id'] == turn_id), None)
    if turn is None or item_index >= len(turn.get('items', [])):
        raise ValueError('This item is no longer available; refresh history')
    item = turn['items'][item_index]
    if item.get('type') in ('reasoning', 'thinking', 'redacted_thinking'):
        raise ValueError('Internal reasoning is not available')
    text = item_text(item)
    if hashlib.sha256(text.encode()).hexdigest() != item_key:
        raise ValueError('This item changed; refresh history before opening details')
    if offset > len(text):
        raise ValueError('Invalid detail position')
    end = min(offset + DETAIL_CHARS, len(text))
    return {'turn_id': turn_id, 'item_key': item_key, 'text': text[offset:end], 'offset': offset,
            'nextOffset': end if end < len(text) else None, 'total': len(text)}


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
