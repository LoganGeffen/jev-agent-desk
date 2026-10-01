from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import model_stream


class ModelStreamTests(unittest.TestCase):
    def client(self, status='completed', error=None, notifications=()):
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
            *notifications,
            {'method': 'turn/completed', 'params': {'threadId': 'observer-only', 'turn': {'status': status, 'error': error}}},
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

    def test_failed_turn_reports_provider_reason(self):
        connection, _, _ = self.client('failed', error={'message': 'Rate limit exceeded'})
        with patch.object(model_stream, 'connection', connection):
            with self.assertRaisesRegex(RuntimeError, 'Spoken answer did not complete: Rate limit exceeded'):
                model_stream.stream_answer('Status?', {}, 'Observe.', 'fixture')

    def test_failed_turn_preserves_error_notification_when_final_error_is_missing(self):
        notification = {'method': 'error', 'params': {'threadId': 'observer-only',
                        'error': {'message': 'Connection lost'}, 'willRetry': False}}
        connection, _, _ = self.client('failed', notifications=[notification])
        with patch.object(model_stream, 'connection', connection):
            with self.assertRaisesRegex(RuntimeError, 'Spoken answer did not complete: Connection lost'):
                model_stream.stream_answer('Status?', {}, 'Observe.', 'fixture')

    def test_final_error_takes_precedence_over_intermediate_retry_error(self):
        notification = {'method': 'error', 'params': {'threadId': 'observer-only',
                        'error': {'message': 'Reconnecting'}, 'willRetry': True}}
        connection, _, _ = self.client('failed', error={'message': 'Provider unavailable'}, notifications=[notification])
        with patch.object(model_stream, 'connection', connection):
            with self.assertRaisesRegex(RuntimeError, 'Spoken answer did not complete: Provider unavailable'):
                model_stream.stream_answer('Status?', {}, 'Observe.', 'fixture')

    def test_recovered_provider_error_does_not_fail_completed_turn(self):
        notification = {'method': 'error', 'params': {'threadId': 'observer-only',
                        'error': {'message': 'Reconnecting'}, 'willRetry': True}}
        connection, _, _ = self.client(notifications=[notification])
        with patch.object(model_stream, 'connection', connection):
            result = model_stream.stream_answer('Status?', {}, 'Observe.', 'fixture')
        self.assertEqual(result['text'], 'First. Last.')

    def test_unrelated_thread_error_is_ignored(self):
        notification = {'method': 'error', 'params': {'threadId': 'unrelated',
                        'error': {'message': 'Unrelated failure'}, 'willRetry': False}}
        connection, _, _ = self.client('failed', notifications=[notification])
        with patch.object(model_stream, 'connection', connection):
            with self.assertRaises(RuntimeError) as raised:
                model_stream.stream_answer('Status?', {}, 'Observe.', 'fixture')
        self.assertEqual(str(raised.exception), 'Spoken answer did not complete')
