import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_actions
from reply_watch import matching_reply


FIXTURE = r'''
import json,sys
config=json.load(open(sys.argv[1]))
for line in sys.stdin:
    request=json.loads(line)
    with open(sys.argv[2], 'a') as log:
        log.write(json.dumps(request)+'\n')
    if 'id' not in request:
        continue
    method=request['method']
    params=request['params']
    response={'id':request['id']}
    if method=='initialize':
        response['result']={}
    elif method=='thread/read':
        if config.get('missing'):
            response['error']={'code':-1,'message':'thread not loaded: fixture'}
        elif config.get('error'):
            response['error']={'code':-1,'message':config['error']}
        elif params['includeTurns'] and config.get('mode')=='paginated':
            response['error']={'code':-1,'message':'paginated threads do not support thread/read(includeTurns=true)'}
        else:
            response['result']={'thread':{'id':'fixture','historyMode':config.get('mode','legacy'),
                'turns':config.get('turns',[]) if params['includeTurns'] else []}}
    elif method=='thread/turns/list':
        if params['itemsView']!='full' or params['sortDirection']!='asc':
            response['error']={'code':-1,'message':'Full chronological history required'}
        else:
            cursor=params.get('cursor') or 'first'
            value=config['pages'][cursor]
            if 'error' in value:
                response['error']={'code':-1,'message':value['error']}
            else:
                response['result']=value
    else:
        response['error']={'code':-1,'message':'Unexpected mutation or method'}
    print(json.dumps({'method':'fixture/notification','params':{}}),flush=True)
    print(json.dumps(response),flush=True)
'''


def turn(id, text, status='completed'):
    return {'id': id, 'status': status, 'itemsView': 'full', 'items': [
        {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Exact question.'}]},
        {'type': 'agentMessage', 'phase': 'final_answer', 'text': text}]}


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='jev-history-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fixture = self.root / 'provider.py'
        self.fixture.write_text(FIXTURE)
        self.config = self.root / 'config.json'
        self.log = self.root / 'requests.jsonl'
        real_popen = subprocess.Popen
        def launch(args, **kwargs):
            self.assertEqual(args, ['codex', 'app-server'])
            self.assertNotIn('TYPESAFE_API_KEY', kwargs['env'])
            return real_popen([sys.executable, str(self.fixture), str(self.config), str(self.log)], **kwargs)
        mock = patch.object(codex_actions.subprocess, 'Popen', side_effect=launch)
        mock.start()
        self.addCleanup(mock.stop)

    def read(self, config):
        self.config.write_text(json.dumps(config))
        return codex_actions.thread_read('fixture')

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_paginated_history_preserves_full_items_order_and_completed_reply(self):
        old, new, running = turn('old', 'Old reply.'), turn('new', 'Exact new reply.'), turn('running', 'Partial', 'inProgress')
        result = self.read({'mode': 'paginated', 'pages': {
            'first': {'data': [old], 'nextCursor': 'page-two'},
            'page-two': {'data': [new, running], 'nextCursor': None}}})
        self.assertEqual(result['turns'], [old, new, running])
        self.assertEqual(codex_actions.latest_reply(result), 'Exact new reply.')
        matched = matching_reply({'turns': result['turns'][:2]}, {'old'}, 'Exact question.')
        self.assertEqual(matched, {'turn_id': 'new', 'text': 'Exact new reply.'})
        requests = self.requests()
        self.assertEqual([r['method'] for r in requests], [
            'initialize', 'initialized', 'thread/read', 'thread/turns/list', 'thread/turns/list'])
        self.assertEqual(requests[-1]['params']['cursor'], 'page-two')
        self.assertEqual(len({r['id'] for r in requests if 'id' in r}), 4)
        self.assertFalse(requests[2]['params']['includeTurns'])

    def test_legacy_history_keeps_existing_readback(self):
        reply = turn('old', 'Existing reply.')
        result = self.read({'mode': 'legacy', 'turns': [reply]})
        self.assertEqual(result['turns'], [reply])
        self.assertTrue(self.requests()[-1]['params']['includeTurns'])

    def test_new_thread_with_no_persisted_history_remains_explicit(self):
        self.assertEqual(self.read({'missing': True}), {'id': 'fixture', 'turns': [], 'history_missing': True})

    def test_empty_paginated_thread_is_not_an_error(self):
        result = self.read({'mode': 'paginated', 'pages': {'first': {'data': [], 'nextCursor': None}}})
        self.assertEqual(result['turns'], [])

    def test_failed_page_never_returns_partial_history(self):
        with self.assertRaisesRegex(RuntimeError, 'page unavailable'):
            self.read({'mode': 'paginated', 'pages': {
                'first': {'data': [turn('old', 'Old')], 'nextCursor': 'next'},
                'next': {'error': 'page unavailable'}}})

    def test_summary_items_and_repeated_cursors_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'incomplete turn items'):
            self.read({'mode': 'paginated', 'pages': {'first': {
                'data': [{**turn('old', 'Summary'), 'itemsView': 'summary'}]}}})
        with self.assertRaisesRegex(RuntimeError, 'did not advance'):
            self.read({'mode': 'paginated', 'pages': {
                'first': {'data': [], 'nextCursor': 'same'},
                'same': {'data': [], 'nextCursor': 'same'}}})

    def test_unrelated_provider_error_is_not_reinterpreted_as_missing_history(self):
        with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
            self.read({'error': 'Permission denied'})
