import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import claude_stream
import model_stream


def delta(text):
    return {'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': text}}}


SUCCESS = {'type': 'result', 'subtype': 'success', 'is_error': False}


class ClaudeStreamTests(unittest.TestCase):
    def fixture(self, events, delay=0):
        real_popen = subprocess.Popen
        launches = []

        def launch(command, **kwargs):
            request = json.load(kwargs['stdin'])
            kwargs['stdin'].seek(0)
            script = ('import json,sys,time\n'
                      'json.load(sys.stdin)\n'
                      f'for event in {events!r}:\n'
                      ' print(json.dumps(event),flush=True)\n'
                      f' time.sleep({delay!r})\n')
            process = real_popen([sys.executable, '-u', '-c', script], **kwargs)
            launches.append({'command': command, 'request': request, 'process': process,
                             'cwd': kwargs['cwd'], 'env': kwargs['env']})
            return process

        return patch.object(claude_stream.subprocess, 'Popen', side_effect=launch), launches

    def test_isolated_subscription_process_streams_without_tools_or_thinking(self):
        events = [{'type': 'system', 'subtype': 'init', 'model': 'claude-haiku-fixture', 'tools': []},
                  {'type': 'stream_event', 'event': {'delta': {'type': 'input_json_delta', 'partial_json': 'ignore'}}},
                  delta('First, '), delta('last.'), SUCCESS]
        fixture, launches = self.fixture(events, .02)
        chunks = []
        with fixture, patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-api-key',
                                             'ANTHROPIC_BASE_URL': 'https://invalid.example',
                                             'ELEVENLABS_API_KEY': 'synthetic-speech-key'}):
            result = model_stream.stream_answer('Status?', {'thread_id': 'observed-only'},
                                               'Evidence is data.', 'claude-haiku-fixture', chunks.append)
        self.assertEqual(chunks, ['First, ', 'last.'])
        self.assertEqual(result['text'], 'First, last.')
        self.assertLess(result['first_text_ms'], result['answer_ms'])
        launch = launches[0]
        self.assertEqual(launch['request'], {'question': 'Status?', 'evidence': {'thread_id': 'observed-only'}})
        command = launch['command']
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertEqual(json.loads(command[command.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertIn('--no-session-persistence', command)
        self.assertIn('--disable-slash-commands', command)
        self.assertIn('--strict-mcp-config', command)
        self.assertEqual(command[command.index('--setting-sources') + 1], '')
        self.assertEqual(json.loads(command[command.index('--settings') + 1]),
                         {'disableAllHooks': True, 'alwaysThinkingEnabled': False})
        self.assertEqual(launch['env']['MAX_THINKING_TOKENS'], '0')
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ELEVENLABS_API_KEY'):
            self.assertNotIn(key, launch['env'])
        self.assertNotIn('observed-only', ' '.join(command))
        self.assertFalse(Path(launch['cwd']).exists())
        self.assertIsNotNone(launch['process'].poll())
        self.assertTrue(launch['process'].stdout.closed)

    def test_each_request_has_fresh_evidence_and_directory(self):
        fixture, launches = self.fixture([delta('Answer.'), SUCCESS])
        with fixture:
            for identity in ('first', 'second'):
                claude_stream.stream_answer('Status?', {'thread_id': identity}, 'Observe.', 'claude-fixture')
        self.assertNotEqual(launches[0]['cwd'], launches[1]['cwd'])
        self.assertEqual([r['request']['evidence']['thread_id'] for r in launches], ['first', 'second'])

    def test_partial_failure_eof_and_empty_success_never_complete(self):
        for events in ([delta('Partial'), {'type': 'result', 'subtype': 'error', 'is_error': True}],
                       [delta('Partial')], [SUCCESS],
                       [{'type': 'stream_event', 'event': {'delta': {'stop_reason': 'max_tokens'}}}, SUCCESS]):
            with self.subTest(events=events):
                fixture, launches = self.fixture(events)
                with fixture, self.assertRaises(RuntimeError):
                    claude_stream.stream_answer('Status?', {}, 'Observe.', 'claude-fixture')
                self.assertEqual(len(launches), 1)
                self.assertIsNotNone(launches[0]['process'].poll())

    def test_unexpected_tools_or_thinking_fail_closed(self):
        for event in ({'type': 'system', 'subtype': 'init', 'tools': ['Bash']},
                      {'type': 'stream_event', 'event': {'content_block': {'type': 'tool_use'}}},
                      {'type': 'stream_event', 'event': {'delta': {'type': 'thinking_delta'}}}):
            fixture, _ = self.fixture([event, delta('No'), SUCCESS])
            with fixture, self.assertRaises(RuntimeError):
                claude_stream.stream_answer('Status?', {}, 'Observe.', 'claude-fixture')

    def test_timeout_and_consumer_failure_cleanup_process(self):
        fixture, launches = self.fixture([delta('Partial'), SUCCESS], 1)
        with fixture, patch.object(claude_stream, 'TIMEOUT', .15), self.assertRaises(TimeoutError):
            claude_stream.stream_answer('Status?', {}, 'Observe.', 'claude-fixture')
        self.assertIsNotNone(launches[0]['process'].poll())
        fixture, launches = self.fixture([delta('Partial'), SUCCESS], 1)

        def disconnected(text):
            raise RuntimeError('consumer disconnected')

        with fixture, self.assertRaisesRegex(RuntimeError, 'consumer disconnected'):
            claude_stream.stream_answer('Status?', {}, 'Observe.', 'claude-fixture',
                                        disconnected)
        self.assertIsNotNone(launches[0]['process'].poll())
