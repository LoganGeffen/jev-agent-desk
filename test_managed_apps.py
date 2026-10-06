import base64
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from browser_http import browser_owner
from managed_apps import ManagedApps
from test_visual_workspace import VisualTests as VisualFixture


class FakeBrowser:
    def __init__(self):
        self.pages = []
        self.fail = None

    def request(self, op, **args):
        if self.fail == op:
            raise RuntimeError('Browser restarted')
        if op == 'open':
            page = {'page_id': args['profile_id'], 'profile_id': args['profile_id'], 'run_id': 'fixture', 'account_epoch': 0, 'status': 'ready'}
            self.pages.append(page)
            return page
        if op == 'pages':
            return {'pages': self.pages}
        if op == 'action':
            if args['action'] == 'screenshot':
                return {'result': {'data': base64.b64encode(b'synthetic screenshot').decode()}}
            self.pages = [page for page in self.pages if page['page_id'] != args['page_id']]
        return {}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.fixture = VisualFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.app
        self.app.visual = self.fixture.visual
        self.app.browser = FakeBrowser()
        self.apps = ManagedApps(self.app)
        self.owner = browser_owner(self.app.visual, self.fixture.target)
        self.workspace = self.fixture.root / 'workspace'
        self.workspace.mkdir()
        code = "import os,json;from http.server import HTTPServer,SimpleHTTPRequestHandler;from pathlib import Path;Path('environment.json').write_text(json.dumps({'secret_present':'TYPESAFE_API_KEY' in os.environ,'home':os.environ['HOME']}));HTTPServer(('127.0.0.1',int(os.environ['PORT'])),SimpleHTTPRequestHandler).serve_forever()"
        self.command = [sys.executable, '-c', code, '{port}']

    def start_app(self):
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-secret-must-not-inherit'}):
            result = self.apps.start(self.owner, str(self.workspace), {'title': 'Test app', 'command': self.command})
        self.addCleanup(lambda: self.apps.stop(self.owner, result['id']))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = self.apps.records[result['id']]
            if record['status'] != 'starting':
                self.assertEqual(record['status'], 'ready', record)
                return record
            time.sleep(.05)
        self.fail('App readiness never settled')

    def test_isolated_environment_real_listener_and_immutable_snapshot_survive_stop(self):
        record = self.start_app()
        environment = json.loads((self.workspace / 'environment.json').read_text())
        self.assertFalse(environment['secret_present'])
        self.assertNotEqual(environment['home'], os.environ['HOME'])
        receipt = record['saved_export']
        self.assertIsNotNone(receipt)
        self.apps.stop(self.owner, record['id'])
        self.assertEqual(record['status'], 'stopped')
        self.assertEqual(self.app.visual.content(receipt['workspace_id'], receipt['version_id'])[0], b'synthetic screenshot')
        version = self.app.visual.list('@1')['creations'][0]['versions'][0]
        self.assertEqual(version['source']['app']['representation'], 'screenshot')
        self.assertEqual(version['source']['app']['process'], record['source']['process'])

    def test_stopping_still_terminates_exact_app_after_browser_restart_or_snapshot_failure(self):
        for failing_op in ('pages', 'action'):
            with self.subTest(failing_op=failing_op):
                self.app.browser.fail = None
                record = self.start_app()
                self.app.browser.fail = failing_op
                result = self.apps.stop(self.owner, record['id'])
                self.assertEqual(result['status'], 'stopped')
                self.assertIn('failed' if failing_op == 'pages' else 'not saved', result['error'])
                deadline = time.monotonic() + 3
                while self.apps.alive(record) and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(self.apps.alive(record))
