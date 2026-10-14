import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation_history import DETAIL_CHARS, claude_page, display_turns, read_details, reverse_lines


class ConversationHistoryTests(unittest.TestCase):
    def test_chat_text_is_exact_and_large_tool_payloads_are_bounded(self):
        message = {'type': 'agentMessage', 'text': 'Exact  wording.\n' * 1000}
        result = display_turns([{'id': 'turn', 'items': [message,
            {'type': 'mcpToolCall', 'tool': 'fixture', 'result': 'x' * 100000},
            {'type': 'reasoning', 'text': 'internal'}]}])[0]
        self.assertEqual(result['items'][0], message)
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(result['items'][1]['shortened'])
        self.assertEqual(len(result['items'][1]['preview']), 2000)
    def test_reverse_lines_crosses_blocks_and_preserves_unterminated_tail(self):
        lines = [b'a' * 70000, b'short', b'last']
        result = list(reverse_lines(io.BytesIO(b'\n'.join(lines))))
        self.assertEqual([line for _, line in result], list(reversed(lines)))
        self.assertEqual([offset for offset, _ in result], [70007, 70001, 0])

    def test_claude_pages_follow_parent_chain_without_sidechain_or_size_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / '.claude/projects/project'
            root.mkdir(parents=True)
            path = root / 'session.jsonl'
            rows, parent = [], None
            for i in range(5):
                user, assistant = f'user-{i}', f'assistant-{i}'
                rows.extend([
                    {'uuid': user, 'parentUuid': parent, 'type': 'user', 'message': {'content': f'Question {i}'}},
                    {'uuid': assistant, 'parentUuid': user, 'type': 'assistant',
                     'message': {'content': [{'type': 'text', 'text': f'Answer {i}'}], 'stop_reason': 'end_turn'}},
                    {'uuid': f'side-{i}', 'isSidechain': True, 'parentUuid': assistant, 'type': 'user', 'message': {'content': 'Ignore'}}])
                parent = assistant
            path.write_text('\n'.join(map(json.dumps, rows)) + '\n{"partial":')
            binding = {'session_id': 'session', 'transcript_path': str(path)}
            with patch('conversation_history.Path.home', return_value=home):
                page = claude_page(binding, None, 2)
                self.assertEqual([turn['id'] for turn in page['turns']], ['user-3', 'user-4'])
                page = claude_page(binding, page['nextCursor'], 2)
                self.assertEqual([turn['id'] for turn in page['turns']], ['user-1', 'user-2'])
                page = claude_page(binding, page['nextCursor'], 2)
                self.assertEqual([turn['id'] for turn in page['turns']], ['user-0'])
                self.assertIsNone(page['nextCursor'])

    def test_details_reassemble_complete_output_and_use_original_page_and_index(self):
        turns = [{'id': 'turn', 'items': [{'type': 'reasoning', 'text': 'private'},
            {'type': 'mcpToolCall', 'result': 'a☃' * 50000}]}]
        shown = display_turns(turns, before='older')[0]['items'][0]
        self.assertEqual(shown['detail_ref']['item_index'], 1)
        chunks, offset = [], 0
        with patch('conversation_history.read_page', return_value={'turns': turns}) as read:
            while offset is not None:
                result = read_details('codex', 'thread', **shown['detail_ref'], offset=offset)
                self.assertLessEqual(len(result['text']), DETAIL_CHARS)
                chunks.append(result['text'])
                offset = result['nextOffset']
            read.assert_called_with('codex', 'thread', None, 'older')
        self.assertEqual(json.loads(''.join(chunks)), turns[0]['items'][1])

    def test_details_reject_changed_missing_and_hidden_items(self):
        turns = [{'id': 'turn', 'items': [{'type': 'mcpToolCall', 'result': 'old'}]}]
        ref = display_turns(turns)[0]['items'][0]['detail_ref']
        with patch('conversation_history.read_page', return_value={'turns': turns}):
            turns[0]['items'][0]['result'] = 'new'
            with self.assertRaisesRegex(ValueError, 'changed'):
                read_details('codex', 'thread', **ref)
            turns[0]['items'][0] = {'type': 'reasoning', 'text': 'private'}
            with self.assertRaisesRegex(ValueError, 'Internal reasoning'):
                read_details('codex', 'thread', **ref)
            turns.clear()
            with self.assertRaisesRegex(ValueError, 'no longer available'):
                read_details('codex', 'thread', **ref)

    def test_attachment_metadata_and_visible_status_without_binary_or_reasoning(self):
        item = {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Exact  words'},
            {'type': 'image', 'source': {'media_type': 'image/png', 'data': 'PRIVATE_BINARY'}},
            {'type': 'localImage', 'path': '/not/read.png'},
            {'type': 'thinking', 'thinking': 'PRIVATE_REASONING'}]}
        result = display_turns([{'id': 'turn', 'status': 'interrupted', 'items': [item]}])[0]
        self.assertEqual(result['status'], 'interrupted')
        content = result['items'][0]['content']
        self.assertEqual(content[0]['text'], 'Exact  words')
        self.assertEqual(content[1]['media_type'], 'image/png')
        self.assertEqual(content[2]['path'], '/not/read.png')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_invalid_detail_requests_do_not_read_provider(self):
        ref = {'turn_id': 'turn', 'item_index': 0, 'item_key': 'a' * 64}
        with patch('conversation_history.read_page') as read:
            for invalid in ({'offset': -1}, {'offset': True}, {'item_index': '0'}, {'item_key': ''}, {'before': []}):
                with self.assertRaises(ValueError):
                    read_details('codex', 'thread', **(ref | invalid))
            read.assert_not_called()
