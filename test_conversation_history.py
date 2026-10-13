import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation_history import claude_page, display_turns, reverse_lines


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
