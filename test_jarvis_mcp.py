import json
import threading
import unittest
import urllib.error
import urllib.request

import jarvis_mcp
from jarvis_mcp import ToolBridge


SEND = {'name': 'send_draft', 'description': 'Send a draft.',
        'input_schema': {'type': 'object', 'required': ['draft_id', 'confirm'], 'properties': {
            'draft_id': {'type': 'string', 'maxLength': 8},
            'confirm': {'type': 'boolean'},
            'mode': {'type': 'string', 'enum': ['now', 'later']},
            'tags': {'type': 'array', 'items': {'type': 'integer'}}}}}
GOOD = {'draft_id': 'd-1', 'confirm': True}


class Client:
    def __init__(self, bridge, token=None):
        config = bridge.config()['mcpServers']['jarvis']
        self.url = config['url']
        self.token = token if token is not None else config['headers']['Authorization']
        self.next_id = 0

    def post(self, body, raw=None):
        request = urllib.request.Request(self.url, raw if raw is not None else json.dumps(body).encode(),
                                         {'Authorization': self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                text = response.read()
                return response.status, json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            return error.code, None

    def rpc(self, method, params=None):
        self.next_id += 1
        return self.post({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params or {}})

    def call(self, name, arguments):
        status, body = self.rpc('tools/call', {'name': name, 'arguments': arguments})
        self.assert_ok(status)
        return body['result']

    @staticmethod
    def assert_ok(status):
        assert status == 200, status


class ToolBridgeTests(unittest.TestCase):
    def bridge(self, execute=None, cancel=None, **options):
        self.executed = []

        def default(name, arguments):
            self.executed.append((name, arguments))
            return {'sent': arguments['draft_id']}

        bridge = ToolBridge([SEND], execute or default, cancel, **options)
        bridge.__enter__()
        self.addCleanup(bridge.close)
        return bridge

    def test_exposes_only_application_tools_on_authenticated_loopback(self):
        bridge = self.bridge()
        client = Client(bridge)
        self.assertEqual(bridge._server.server_address[0], '127.0.0.1')
        self.assertEqual(bridge.tool_names, ['mcp__jarvis__send_draft'])
        status, body = client.rpc('initialize', {'protocolVersion': '2025-06-18'})
        self.assertEqual(body['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(client.rpc('tools/list')[1]['result']['tools'],
                         [{'name': 'send_draft', 'description': 'Send a draft.', 'inputSchema': SEND['input_schema']}])
        self.assertEqual(client.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}), (202, None))
        self.assertEqual(client.rpc('resources/list')[1]['error']['code'], -32601)
        for token in ('', 'Bearer wrong'):
            self.assertEqual(Client(bridge, token).rpc('tools/list')[0], 401)
        self.assertEqual(client.post(None, raw=b'not json')[0], 400)
        self.assertEqual(client.post(None, raw=b'[1]')[0], 400)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(client.url, headers={'Authorization': client.token}))
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(self.executed, [])

    def test_valid_call_runs_in_parent_and_records_receipt(self):
        bridge = self.bridge()
        result = Client(bridge).call('send_draft', {**GOOD, 'mode': 'now', 'tags': [1, 2]})
        self.assertEqual(result, {'content': [{'type': 'text', 'text': '{"sent": "d-1"}'}], 'isError': False})
        self.assertEqual(self.executed, [('send_draft', {**GOOD, 'mode': 'now', 'tags': [1, 2]})])
        self.assertEqual(bridge.calls, [{'tool': 'send_draft', 'arguments': {**GOOD, 'mode': 'now', 'tags': [1, 2]},
                                         'result': {'sent': 'd-1'}}])

    def test_invalid_arguments_and_unknown_tools_never_reach_the_application(self):
        bridge = self.bridge(max_calls=20)
        client = Client(bridge)
        bad = [('send_draft', {'draft_id': 'd-1'}), ('send_draft', {**GOOD, 'extra': 1}),
               ('send_draft', {**GOOD, 'confirm': 'yes'}), ('send_draft', {**GOOD, 'mode': 'never'}),
               ('send_draft', {**GOOD, 'draft_id': 'much-too-long'}), ('send_draft', {**GOOD, 'tags': [True]}),
               ('send_draft', {**GOOD, 'tags': [1.5]}), ('send_draft', ['d-1']), ('send_draft', None),
               ('bash', {'command': 'id'}), (['send_draft'], {})]
        for name, arguments in bad:
            result = client.call(name, arguments)
            self.assertTrue(result['isError'], (name, arguments))
            self.assertIn('Do not claim it happened', result['content'][0]['text'])
        self.assertEqual(self.executed, [])
        self.assertEqual(len(bridge.calls), len(bad))
        self.assertTrue(all('invalid call' in call['error'] for call in bridge.calls))

    def test_application_failure_is_an_error_result_not_a_success(self):
        def fail(name, arguments):
            raise PermissionError('identity changed')

        bridge = self.bridge(fail)
        result = Client(bridge).call('send_draft', GOOD)
        self.assertTrue(result['isError'])
        self.assertEqual(bridge.calls[0]['error'], 'PermissionError: identity changed')

    def test_non_json_receipt_is_reported_as_error(self):
        bridge = self.bridge(lambda name, arguments: {'bad': object()})
        self.assertTrue(Client(bridge).call('send_draft', GOOD)['isError'])
        self.assertIn('TypeError', bridge.calls[0]['error'])

    def test_cancel_before_dispatch_blocks_the_callback(self):
        cancel = threading.Event()
        bridge = self.bridge(cancel=cancel)
        cancel.set()
        self.assertTrue(Client(bridge).call('send_draft', GOOD)['isError'])
        self.assertEqual(self.executed, [])
        self.assertEqual(bridge.calls[0]['error'], 'cancelled before dispatch')

    def test_call_limit(self):
        bridge = self.bridge(max_calls=2)
        client = Client(bridge)
        self.assertEqual([client.call('send_draft', GOOD)['isError'] for _ in range(3)], [False, False, True])
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(bridge.calls[2]['error'], 'tool call limit reached')

    def test_close_waits_for_inflight_action_and_refuses_later_calls(self):
        entered, release = threading.Event(), threading.Event()

        def slow(name, arguments):
            entered.set()
            release.wait(5)
            return {'sent': True}

        bridge = ToolBridge([SEND], slow)
        bridge.__enter__()
        client = Client(bridge)
        responses = []
        threading.Thread(target=lambda: responses.append(client.call('send_draft', GOOD))).start()
        self.assertTrue(entered.wait(5))
        closer = threading.Thread(target=bridge.close)
        closer.start()
        closer.join(.3)
        self.assertTrue(closer.is_alive())
        release.set()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertEqual(bridge.calls[0]['result'], {'sent': True})
        self.assertTrue(bridge._closed)

    def test_specs_are_validated_up_front(self):
        def spec(name='tool', schema=None):
            return {'name': name, 'description': 'd', 'input_schema': schema or {'type': 'object'}}

        for specs in ([], [spec('bad name')], [spec('a'), spec('a')], [spec('x' * 41)],
                      [spec(schema={'type': 'array'})],
                      [spec(schema={'type': 'object', 'properties': {'a': {'type': 'string', 'pattern': '.*'}}})],
                      [spec(schema={'type': 'object', 'properties': {'a': {'type': 'null'}}})],
                      [spec(schema={'type': 'object', 'oneOf': []})]):
            with self.assertRaises(ValueError, msg=specs):
                ToolBridge(specs, lambda *args: {})
        with self.assertRaises(ValueError):
            jarvis_mcp.check_schema({'type': 'object', 'additionalProperties': True})


if __name__ == '__main__':
    unittest.main()
