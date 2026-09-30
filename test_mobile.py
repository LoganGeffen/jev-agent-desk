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
        self.app.evaluate = lambda payload: {'answers': {'action': {'choice': 'no_action'}}}
        server = make_server(self.app)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        body = {'request': 'fixture', 'request_id': 'fixture-request-id'}
        with urlopen(Request(url + '/api/request', data=json.dumps(body).encode(),
                             headers={'Content-Type': 'application/json'})) as response:
            event = json.load(response)
        with urlopen(url + '/api/state') as response:
            latest = json.load(response)['latest']
        self.assertEqual(event['outcome'], 'ok')
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
