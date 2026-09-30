from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import model_stream


class ModelStreamTests(unittest.TestCase):
    def client(self, status='completed'):
        calls, sent = [], []
        client = SimpleNamespace(sequence=0, overrides={'mcp_servers.fixture.enabled': False},
                                 directory=SimpleNamespace(name='/tmp/synthetic-answer'))
        def call(method, params):
            calls.append((method, params))
            return {'thread': {'id': 'observer-only'}}
        events = iter([
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'unrelated', 'delta': 'Ignore'}},
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'observer-only', 'delta': 'First. '}},
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'observer-only', 'delta': 'Last.'}},
            {'method': 'turn/completed', 'params': {'threadId': 'observer-only', 'turn': {'status': status}}},
        ])
        client.call = call
        client.send = lambda *args: sent.append(args)
        client.receive = lambda: next(events)
        @contextmanager
        def connection():
            yield client
        return connection, calls, sent

    def test_observation_uses_new_ephemeral_thread_and_streams_only_its_text(self):
        connection, calls, sent = self.client()
        chunks = []
        with patch.object(model_stream, 'connection', connection):
            result = model_stream.stream_answer('Status?', {'thread_id': 'real-session'}, 'Observe only.', 'fixture', chunks.append)
        self.assertEqual(chunks, ['First. ', 'Last.'])
        self.assertEqual(result['text'], 'First. Last.')
        self.assertEqual(calls[0][0], 'thread/start')
        self.assertTrue(calls[0][1]['ephemeral'])
        self.assertEqual(calls[0][1]['sandbox'], 'read-only')
        self.assertFalse(calls[0][1]['config']['mcp_servers.fixture.enabled'])
        self.assertEqual(sent[0][1]['threadId'], 'observer-only')
        self.assertEqual(calls[-1][0], 'thread/unsubscribe')

    def test_partial_output_never_turns_a_failed_model_turn_into_success(self):
        connection, _, _ = self.client('failed')
        with patch.object(model_stream, 'connection', connection):
            with self.assertRaisesRegex(RuntimeError, 'did not complete'):
                model_stream.stream_answer('Status?', {}, 'Observe.', 'fixture')
