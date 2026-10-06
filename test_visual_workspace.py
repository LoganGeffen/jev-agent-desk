import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest
from unittest.mock import patch

from server import make_server
from visual import capture
from visual_workspace import VisualError, VisualWorkspace, endpoint_identity, safe_path


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.panes = {'%1': {'provider': 'codex', 'thread_id': 'conversation-a', 'process': '100:200', 'identity': 'exact-a'},
                      '%2': {'provider': 'claude', 'thread_id': 'conversation-b', 'process': '300:400', 'identity': 'exact-b'}}
        self.owner_generation = 'server-generation-one'
        self.app = SimpleNamespace(log=self.root / 'events.jsonl', validate_pane=self.validate_pane)
        self.owner_patch = patch.object(VisualWorkspace, 'owner', side_effect=lambda tab: self.owner_generation + tab)
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        agent_patch = patch('visual_workspace.agent_process', side_effect=lambda pane: pane['process'])
        agent_patch.start()
        self.addCleanup(agent_patch.stop)
        self.visual = VisualWorkspace(self.app)
        self.target = {'tab': '@1', 'pane': '%1', 'identity': 'exact-a'}

    def validate_pane(self, tab, pane, identity):
        value = self.panes.get(pane)
        if value is None or value['identity'] != identity or (tab, pane) not in (('@1', '%1'), ('@2', '%2')):
            raise ValueError('stale')
        return value.copy()

    def body(self, data=b'<h1>First</h1>', **changes):
        result = {'target': self.target.copy(), 'title': 'Example', 'kind': 'html', 'mode': 'captured', 'entrypoint': 'index.html',
                  'files': [{'path': 'index.html', 'data': base64.b64encode(data).decode()}]}
        result.update(changes)
        return result

    def start_http(self):
        server = make_server(self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        self.visual = self.app.visual
        return 'http://127.0.0.1:' + str(server.server_port)

    def test_versions_survive_source_deletion_restart_and_server_incarnation(self):
        source = self.root / 'source.html'
        source.write_text('<h1>Original</h1>')
        entrypoint, files = capture(source)
        first = self.visual.register(self.body(entrypoint=entrypoint, files=files))
        source.write_text('<h1>Changed</h1>')
        entrypoint, files = capture(source)
        second = self.visual.register(self.body(entrypoint=entrypoint, files=files, creation_id=first['creation_id']))
        source.unlink()
        restarted = VisualWorkspace(self.app)
        workspace = restarted.list('@1')
        self.assertEqual([v['number'] for v in workspace['creations'][0]['versions']], [2, 1])
        self.assertEqual(restarted.content(first['workspace_id'], first['version_id'])[0], b'<h1>Original</h1>')
        self.assertEqual(restarted.content(first['workspace_id'], second['version_id'])[0], b'<h1>Changed</h1>')
        self.assertEqual(restarted.list('@2')['creations'], [])
        self.owner_generation = 'replacement-server'
        self.assertEqual(restarted.list('@1')['creations'], [])
        self.assertEqual(restarted.content(first['workspace_id'], first['version_id'])[0], b'<h1>Original</h1>')

    def test_cross_workspace_and_changed_conversation_rejected(self):
        first = self.visual.register(self.body())
        target = {'tab': '@2', 'pane': '%2', 'identity': 'exact-b'}
        with self.assertRaises(VisualError):
            self.visual.register(self.body(target=target, creation_id=first['creation_id']))
        with self.assertRaises(VisualError):
            self.visual.content(self.visual.workspace('@2'), first['version_id'])
        self.panes['%1']['identity'] = 'replaced'
        with self.assertRaises(VisualError):
            self.visual.register(self.body())
        self.assertEqual(len(self.visual.list('@1')['creations']), 1)

    def test_revalidate_after_decode_before_publication(self):
        original = self.visual.target
        calls = []
        def validate(value):
            calls.append(value)
            if len(calls) == 2:
                self.panes['%1']['identity'] = 'replaced'
            return original(value)
        with patch.object(self.visual, 'target', side_effect=validate), self.assertRaises(VisualError):
            self.visual.register(self.body())
        self.assertEqual(self.visual.list('@1')['creations'], [])

    def test_paths_limits_symlinks_and_unknown_input(self):
        for name in ('../secret.txt', '/etc/passwd', '.env.txt', '.run/events.json', 'data/credentials.json', 'id_rsa.txt', 'a\\b.html', 'a/%2e%2e/b.html'):
            with self.subTest(name=name), self.assertRaises(VisualError):
                safe_path(name)
        outside = self.root / 'outside.txt'
        outside.write_text('private')
        link = self.root / 'link.txt'
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            capture(link)
        parent = self.root / 'alias'
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            capture(parent / 'outside.txt')
        with self.assertRaises(VisualError):
            self.visual.register(self.body(unexpected='no'))
        with self.assertRaises(VisualError):
            self.visual.register(self.body(files=self.body()['files'] * 129))
        with self.assertRaises(VisualError):
            self.visual.register(self.body(files=[{'path': 'index.html', 'data': 'not base64'}]))

    def test_http_private_preview_download_and_no_arbitrary_reads(self):
        url = self.start_http()
        registered = self.visual.register(self.body(b'<script>parent.document.body.textContent="bad"</script>'))
        version = self.visual.list('@1')['creations'][0]['versions'][0]
        with urlopen(Request(url + version['preview_url'], headers={'Sec-Fetch-Dest': 'iframe'})) as response:
            policy = response.headers['Content-Security-Policy']
            self.assertIn('sandbox allow-scripts;', policy)
            self.assertNotIn('allow-same-origin', policy)
            self.assertIn("connect-src 'none'", policy)
            self.assertIn("form-action 'none'", policy)
        with urlopen(url + version['download_url']) as response:
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
        for route, headers in ((version['content_url'], {'Origin': 'null'}),
                               ('/api/visual/workspace?tab=%401', {'Origin': 'https://hostile.invalid'}),
                               (version['preview_url'].replace('index.html', '../events.json'), {})):
            with self.subTest(route=route), self.assertRaises(HTTPError):
                urlopen(Request(url + route, headers=headers))
        request = Request(url + '/api/visual/register', data=json.dumps(self.body()).encode(), headers={'Content-Type': 'application/json', 'Origin': 'null'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(registered['workspace_id'], self.visual.workspace('@1'))

    def test_private_ancestry_cannot_be_removed_by_selecting_capture_root(self):
        for directory in ('credentials', 'secrets', 'private-key'):
            folder = self.root / directory
            folder.mkdir()
            source = folder / 'config.json'
            source.write_text('{"fixture":"synthetic-only"}')
            with self.subTest(directory=directory):
                with self.assertRaises(VisualError):
                    capture(source)
                with self.assertRaises(VisualError):
                    capture(folder, 'config.json')

    def test_parent_policy_and_standalone_preview_navigation(self):
        url = self.start_http()
        self.visual.register(self.body())
        version = self.visual.list('@1')['creations'][0]['versions'][0]
        with urlopen(url) as response:
            self.assertEqual(response.headers['Content-Security-Policy'], "frame-src 'self' blob:")
        for destination in ('document', 'empty', ''):
            with self.subTest(destination=destination), self.assertRaises(HTTPError) as error:
                urlopen(Request(url + version['preview_url'], headers={'Sec-Fetch-Dest': destination}))
            self.assertEqual(error.exception.code, 403)

    def test_endpoint_rejects_other_process_and_default_session(self):
        import os
        with self.assertRaises(VisualError):
            endpoint_identity(os.getpid(), 54321, 'disposable')
        with self.assertRaises(VisualError):
            endpoint_identity(os.getpid(), 54321, 'default')

    def test_live_attachment_latches_stale_port_reuse_and_detach_never_controls(self):
        body = {'target': self.target, 'name': 'Preview browser', 'session': 'synthetic-only', 'pid': 123, 'port': 12345}
        with patch('visual_workspace.endpoint_identity', return_value={'process': '123:1', 'inode': 'first'}):
            result = self.visual.attach(body)
            self.assertEqual(self.visual.list('@1')['live']['status'], 'ready')
            self.assertIsNone(self.visual.list('@2')['live'])
        with patch('visual_workspace.endpoint_identity', return_value={'process': '123:2', 'inode': 'second'}):
            self.assertEqual(self.visual.list('@1')['live']['status'], 'stale')
        with patch('visual_workspace.endpoint_identity', return_value={'process': '123:1', 'inode': 'first'}):
            self.assertEqual(self.visual.list('@1')['live']['status'], 'stale')
        self.visual.detach({'tab': '@1', 'attachment_id': result['live']['id']})
        self.assertIsNone(self.visual.list('@1')['live'])

    def test_viewer_close_keeps_ready_but_upstream_failure_requires_reconnect(self):
        body = {'target': self.target, 'name': 'Browser', 'session': 'test', 'pid': 123, 'port': 12345}
        class ClosedViewer:
            def write(self, data):
                raise BrokenPipeError('Viewer left the tab')
        with patch('visual_workspace.endpoint_identity', return_value={'process': '123:1', 'inode': 'same'}):
            result = self.visual.attach(body)
            attachment = result['live']
            handler = SimpleNamespace(send_response=lambda *args: None, send_header=lambda *args: None,
                                      end_headers=lambda: None, wfile=ClosedViewer())
            with patch('visual_workspace.connect') as connect:
                self.visual.stream(handler, result['workspace_id'], attachment['id'], attachment['generation'])
                connect.assert_not_called()
            self.assertEqual(self.visual.list('@1')['live']['status'], 'ready')
            handler.wfile = io.BytesIO()
            with patch('visual_workspace.connect', side_effect=ConnectionResetError('Upstream failed')):
                self.visual.stream(handler, result['workspace_id'], attachment['id'], attachment['generation'])
            self.assertEqual(self.visual.list('@1')['live']['status'], 'disconnected')
            self.assertEqual(self.visual.streaming, set())

    def test_immediate_final_static_frame_is_never_discarded(self):
        frames = [base64.b64encode(b'\xff\xd8\xff' + value).decode() for value in (b'initial', b'final-change')]
        packets = iter(json.dumps({'type': 'frame', 'data': data, 'metadata': {'deviceWidth': 640, 'deviceHeight': 480}}) for data in frames)
        class Upstream:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def recv(self, timeout): return next(packets)
        with patch('visual_workspace.endpoint_identity', return_value={'process': '123:1', 'inode': 'same'}), patch('visual_workspace.connect', return_value=Upstream()):
            result = self.visual.attach({'target': self.target, 'name': 'Browser', 'session': 'test', 'pid': 123, 'port': 12345})
            handler = SimpleNamespace(send_response=lambda *args: None, send_header=lambda *args: None,
                                      end_headers=lambda: None, wfile=io.BytesIO())
            attachment = result['live']
            self.visual.stream(handler, result['workspace_id'], attachment['id'], attachment['generation'])
            events = handler.wfile.getvalue().decode()
            self.assertEqual(events.count('event: frame'), 2)
            self.assertIn(frames[-1], events)

    def test_stream_receives_only_and_fences_late_frames(self):
        url = self.start_http()
        sent = []
        packet = json.dumps({'type': 'frame', 'data': base64.b64encode(b'\xff\xd8\xffimage').decode(),
                             'metadata': {'deviceWidth': 640, 'deviceHeight': 480}})
        test = self
        class Upstream:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def send(self, data): sent.append(data)
            def recv(self, timeout):
                test.panes['%1']['identity'] = 'replaced-after-receive'
                return packet
        with patch('visual_workspace.endpoint_identity', return_value={'process': '123:1', 'inode': 'same'}), patch('visual_workspace.connect', return_value=Upstream()):
            result = self.visual.attach({'target': self.target, 'name': 'Browser', 'session': 'test', 'pid': 123, 'port': 12345})
            with urlopen(url + result['live']['stream_url']) as response:
                events = response.read().decode()
            self.assertIn('"status": "stale"', events)
            self.assertNotIn('event: frame', events)
            self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()
