import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from browser_runtime import BrowserError
from server import make_server
from visual_workspace import VisualError


class BrowserHTTPTests(unittest.TestCase):
    def test_two_observers_lease_origin_identity_and_http_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(log=Path(directory) / 'events.jsonl')
            live_identity = ['a']
            app.panes = lambda tab: [{'id': '%a', 'identity': live_identity[0], 'thread_id': 'thread-a', 'provider': 'codex'}]
            def start(port=0):
                server = make_server(app, port)
                def target(value):
                    if value != {'tab': '@a', 'pane': '%a', 'identity': live_identity[0]}:
                        raise VisualError('Conversation replaced', 409)
                    return {'provider': 'codex', 'thread_id': 'thread-a', 'process': '1:1', 'agent_process': '1:1'}
                app.visual.target = target
                app.visual.workspace = lambda tab: 'workspace-a'
                threading.Thread(target=server.serve_forever, daemon=True).start()
                return server
            server = start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            target = {'tab': '@a', 'pane': '%a', 'identity': 'a'}
            def post(op, **body):
                request = Request(base + '/api/browser/' + op, json.dumps({'target': target, **body}).encode(),
                                  headers={'Content-Type': 'application/json', 'Origin': base})
                with urlopen(request, timeout=10) as response:
                    return json.load(response)
            def frame(stream):
                for _ in range(8):
                    line = stream.readline()
                    if line.startswith(b'data: '):
                        return json.loads(line[6:])
                self.fail('Missing browser frame')
            browser = None
            try:
                page = post('open')
                browser = app.browser
                with urlopen(base + '/api/browser/state?tab=@a') as response:
                    state = json.load(response)
                self.assertEqual(state['pages'][0]['page_id'], page['page_id'])
                self.assertNotIn('owner', state['pages'][0])
                first = urlopen(base + page['stream_url'], timeout=10)
                second = urlopen(base + page['stream_url'], timeout=10)
                try:
                    self.assertEqual(frame(first)['page_id'], page['page_id'])
                    self.assertEqual(frame(second)['page_id'], page['page_id'])
                    self.assertEqual(frame(first)['sequence'], 2)
                    handle = {'page_id': page['page_id'], 'run_id': page['run_id']}
                    lease = post('lease', **handle, client_id='phone')
                    self.assertEqual(lease['status'], 'human')
                    with self.assertRaises(HTTPError) as error:
                        post('action', **handle, account_epoch=0, action='snapshot')
                    self.assertEqual(error.exception.code, 423)
                    post('heartbeat', **handle, client_id='phone', lease_id=lease['lease_id'])
                    post('disconnect', **handle, client_id='phone', lease_id=lease['lease_id'])
                    lease = post('lease', **handle, client_id='desktop')
                    done = post('done', **handle, client_id='desktop', lease_id=lease['lease_id'])
                    self.assertEqual(done['account_status'], 'unknown')
                    self.assertEqual(done['account_epoch'], 1)
                finally:
                    first.close()
                    second.close()
                port = server.server_port
                server.shutdown()
                server.server_close()
                server = start(port)
                with urlopen(base + '/api/browser/state?tab=@a') as response:
                    restored = json.load(response)['pages'][0]
                self.assertEqual(restored['page_id'], page['page_id'])
                self.assertEqual(restored['run_id'], page['run_id'])
                request = Request(base + '/api/browser/state?tab=@a', headers={'Origin': 'https://other.invalid'})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 403)
                live_identity[0] = 'replacement'
                with self.assertRaises(HTTPError) as error:
                    post('action', **handle, account_epoch=1, action='snapshot')
                self.assertEqual(error.exception.code, 409)
            finally:
                server.shutdown()
                server.server_close()
                if browser:
                    browser.shutdown()


if __name__ == '__main__':
    unittest.main()
