import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from jev import ProviderError
from launch import attach_restart, attach_start, attach_status, attach_stop
from server import Playground, make_server, tmux
from voice import Voice
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus


class MobileTests(unittest.TestCase):
    def test_history_http_route_reads_only_validated_conversation(self):
        server = make_server(self.app, public_origin='https://mobile.example')
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        live = self.app.panes(self.window)[0]
        body = {'tab': self.window, 'pane': live['id'], 'identity': live['identity'], 'before': 'older'}
        request = Request(f'http://127.0.0.1:{server.server_port}/api/history',
                          data=json.dumps(body).encode(),
                          headers={'Host': 'mobile.example', 'Origin': 'https://mobile.example', 'Content-Type': 'application/json'})
        with patch.object(self.app, 'validate_pane', return_value={**live, 'provider': 'codex', 'thread_id': 'thread'}) as validate, \
             patch('server.conversation_page', return_value={'turns': [{'id': 'old', 'items': []}], 'nextCursor': 'oldest'}) as read:
            with urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result['identity'], live['identity'])
            self.assertEqual(result['before'], 'oldest')
            self.assertEqual(validate.call_count, 2)
            read.assert_called_once_with('codex', 'thread', None, 'older')
        body['identity'] = 'stale'
        request.data = json.dumps(body).encode()
        with patch('server.conversation_page') as read, self.assertRaises(HTTPError) as rejected:
            urlopen(request)
        self.assertEqual(rejected.exception.code, 400)
        read.assert_not_called()

    def test_scroll_http_route_dispatches_and_rejects_stale_identity(self):
        server = make_server(self.app, public_origin='https://mobile.example')
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        body = {'tab': self.window, 'pane': self.original, 'identity': 'stale', 'direction': 'up'}
        request = Request(f'http://127.0.0.1:{server.server_port}/api/scroll',
                          data=json.dumps(body).encode(),
                          headers={'Host': 'mobile.example', 'Origin': 'https://mobile.example',
                                   'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as rejected:
            urlopen(request)
        self.assertEqual(rejected.exception.code, 400)
        self.assertIn('changed', json.loads(rejected.exception.read())['error'])
        with patch.object(self.app, 'scroll_terminal', return_value={'submitted': True}) as scroll:
            with urlopen(request) as response:
                self.assertEqual(json.load(response), {'submitted': True})
            scroll.assert_called_once_with(**body)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jev-mobile-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.socket = str(self.root / 'tmux.sock')
        tmux(self.socket, '-f', '/dev/null', 'new-session', '-d', '-s', 'source', 'cat')
        self.addCleanup(tmux, self.socket, 'kill-server')
        self.window = tmux(self.socket, 'display-message', '-p', '-t', '=source:', '#{window_id}')
        self.original = tmux(self.socket, 'display-message', '-p', '-t', '=source:', '#{pane_id}')
        self.other = tmux(self.socket, 'split-window', '-d', '-P', '-F', '#{pane_id}', '-t', self.original, 'cat')
        self.app = Playground(self.socket, self.root / 'events.jsonl', source_session='source',
                              independent_selection=True)

    def test_mobile_selection_input_and_closed_pane_preserve_tmux_focus(self):
        state = self.app.select_tab(self.window, self.other)
        self.assertEqual(state['selected_pane'], self.other)
        pane = next(p for p in state['tabs'][0]['panes'] if p['id'] == self.other)
        self.app.terminal_input(self.window, text='mobile fixture', pane_id=self.other, identity=pane['identity'])
        self.assertIn('mobile fixture', tmux(self.socket, 'capture-pane', '-p', '-t', self.other))
        self.assertEqual(tmux(self.socket, 'display-message', '-p', '-t', '=source:', '#{pane_id}'), self.original)
        self.app.close_pane(self.window, self.other, pane['identity'])
        self.assertEqual(self.app.state()['selected_pane'], self.original)

    def test_remote_http_requires_exact_host_and_origin(self):
        origin = 'https://mobile.example.ts.net:8445'
        server = make_server(self.app, public_origin=origin)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        with urlopen(Request(url + '/api/state', headers={'Host': origin[8:]})) as response:
            self.assertEqual(response.status, 200)
        for headers in ({'Host': 'evil.example'}, {'Host': origin[8:], 'Origin': 'https://evil.example'},
                        {'Host': origin[8:]}):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url + '/api/select', data=json.dumps({'tab': self.window}).encode(),
                                headers={**headers, 'Content-Type': 'application/json'}))
            self.assertEqual(error.exception.code, 403)
        with urlopen(Request(url + '/api/select', data=json.dumps({'tab': self.window}).encode(),
                            headers={'Host': origin[8:], 'Origin': origin, 'Content-Type': 'application/json'})) as response:
            self.assertEqual(response.status, 200)

    def test_provider_failure_preserves_diagnostic_evidence_in_event(self):
        details = {'status': 503, 'body': 'upstream unavailable',
                   'headers': {'x-typesafe-request-id': 'req_fixture'}, 'elapsed_ms': 17}
        def fail(payload):
            raise ProviderError(details)
        self.app.evaluate = fail
        event = self.app.submit('fixture')
        self.assertEqual(event['outcome'], 'error')
        self.assertIsNone(event['action'])
        self.assertEqual(event['provider_error'], details)
        self.assertEqual(json.loads(self.app.log.read_text())['provider_error'], details)

    def test_request_id_is_returned_in_polling_state(self):
        self.app.evaluate = lambda payload: {'answers': {'intent': {'choice': 'no_action'}}}
        server = make_server(self.app)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        pane = self.app.panes(self.window)[0]
        body = {'request': 'fixture', 'request_id': 'fixture-request-id',
                'capture_target': {'tab': self.window, 'pane': pane['id'], 'identity': pane['identity']}}
        with self.assertRaises(HTTPError) as rejected:
            urlopen(Request(url + '/api/request', data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json'}))
        event = json.load(rejected.exception)
        with urlopen(url + '/api/state') as response:
            latest = json.load(response)['latest']
        self.assertEqual(event['outcome'], 'error')
        self.assertEqual(event['delivery'], 'not_attempted')
        self.assertEqual(event['request_id'], body['request_id'])
        self.assertEqual(latest['request_id'], body['request_id'])

    def test_voice_accepts_only_configured_origins(self):
        voice = Voice(self.root / 'voice.jsonl')
        voice.relay = lambda client: client.send('connected')
        origin = 'https://mobile.example.ts.net:8445'
        server = voice.start('http://127.0.0.1:12345', public_origin=origin)
        self.addCleanup(server.shutdown)
        self.assertEqual(voice.url, 'wss://mobile.example.ts.net:8445/voice')
        url = f'ws://127.0.0.1:{server.socket.getsockname()[1]}/voice'
        with connect(url, origin=origin) as client:
            self.assertEqual(client.recv(), 'connected')
        with self.assertRaises(InvalidStatus):
            connect(url, origin='https://evil.example')

    def test_restart_retains_configuration_and_in_process_auth(self):
        run = self.root / 'run'
        state = attach_start(self.socket, 'source', 'source', run, reuse_private_auth=False,
                             environment={'PATH': '/usr/bin:/bin', 'TYPESAFE_API_KEY': 'fixture-only',
                                          'ELEVENLABS_API_KEY': 'fixture-only', 'ELEVENLABS_VOICE_ID': 'fixture-only'},
                             independent_selection=True)
        self.addCleanup(lambda: attach_stop(run) if (run / 'owner.json').exists() else None)
        before = attach_status(run)
        self.assertTrue(state['api_configured'])
        with patch.dict('os.environ', {name: '' for name in ('TYPESAFE_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID')}):
            state = attach_restart(run)
        self.assertTrue(state['api_configured'])
        self.assertTrue(state['voice']['configured'])
        after = attach_status(run)
        self.assertNotEqual(before['pid'], after['pid'])
        self.assertTrue(after['independent_selection'])
        self.assertNotIn('fixture-only', (run / 'owner.json').read_text())


if __name__ == '__main__':
    unittest.main()


class SubmitKeyTests(unittest.TestCase):
    setUp = MobileTests.setUp
    def test_message_delivery_pastes_literal_text_and_submits_with_one_call(self):
        import codex_actions
        import time
        agent = {'socket': self.socket, 'window_id': self.window, 'pane_id': self.other,
                 'preserve_focus': True}
        with patch.object(codex_actions, 'check_target'):
            codex_actions.send_message(self.socket, agent, 'ONE_SEND_TEST')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            screen = tmux(self.socket, 'capture-pane', '-p', '-t', self.other)
            if screen.count('ONE_SEND_TEST') == 2:
                break
            time.sleep(.03)
        self.assertEqual(screen.count('ONE_SEND_TEST'), 2, 'cat must receive the completed line after Enter')
