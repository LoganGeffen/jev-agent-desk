import threading
import time
import unittest
from unittest.mock import patch

from jarvis_store import JarvisStore
from test_jarvis_tools import DisposableDesk, final


class JobTests(DisposableDesk):
    def gated(self):
        gate = {'entered': threading.Semaphore(0), 'release': threading.Event(), 'calls': 0}
        def slow(*args, **kwargs):
            gate['calls'] += 1
            gate['entered'].release()
            gate['release'].wait(5)
            return {'text': 'Observer evidence', 'snapshot': {'observed_at': 'now'}}
        return gate, slow

    def queue(self, name='Dog Data', request='Observe', request_id=None):
        self.provider = lambda context, specs, execute, **kwargs: execute('observer_job', {'name': name}) and final()
        return self.app.jarvis.submit(request, request_id or request)

    def job_ids(self):
        return list(self.app.jarvis.snapshot()['jobs'])

    def wait_status(self, job_id, status):
        deadline = time.monotonic() + 3
        while self.app.jarvis.snapshot()['jobs'][job_id]['status'] != status:
            self.assertLess(time.monotonic(), deadline, self.app.jarvis.snapshot()['jobs'][job_id])
            time.sleep(.01)

    def notices(self):
        return list(self.app.jarvis.snapshot()['notices'].values())

    def job_status(self, job_id, request_id):
        results = []
        self.provider = lambda context, specs, execute, **kwargs: results.append(execute('job_status', {'job_id': job_id})) or final()
        receipt = self.app.jarvis.submit('How is that observation?', request_id)
        return results[-1], receipt

    def test_slow_job_is_tracked_queryable_and_completion_is_one_quiet_exact_notice(self):
        gate, slow = self.gated()
        with patch('jarvis_tools.ask_session', side_effect=slow):
            started = time.monotonic()
            first = self.queue()
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(gate['entered'].acquire(timeout=1))
            (job_id,) = self.job_ids()
            job = self.app.jarvis.snapshot()['jobs'][job_id]
            self.assertEqual((job['status'], job['subject']), ('running', self.targets['Dog Data']))
            self.assertNotIn('done', first['text'])
            self.provider = lambda context, specs, execute, **kwargs: execute('read_reply', {'name': 'Controller'}) and final()
            follow = self.app.jarvis.submit('Meanwhile read Controller', 'follow')
            self.assertEqual(follow['status'], 'completed')
            self.assertEqual(self.app.jarvis.snapshot()['subject'], self.targets['Controller'])
            running, receipt = self.job_status(job_id, 'while-running')
            self.assertEqual((running['job_status'], running['job_id']), ('running', job_id))
            self.assertIn('still running', receipt['text'])
            turns_before = self.app.jarvis.snapshot()["turns"]
            gate['release'].set()
            self.wait_status(job_id, 'completed')
        deadline = time.monotonic() + 2
        while not self.notices() and time.monotonic() < deadline:
            time.sleep(.01)
        (notice,) = self.notices()
        self.assertEqual((notice['id'], notice['job_id'], notice['subject'], notice['status'], notice['quiet']),
                         ('job:' + job_id, job_id, self.targets['Dog Data'], 'completed', True))
        self.assertEqual(notice['result_ref'], {'job_id': job_id})
        snapshot = self.app.jarvis.snapshot()
        self.assertEqual(snapshot['subject'], self.targets['Controller'])
        self.assertEqual(snapshot["turns"].keys(), turns_before.keys())
        self.assertEqual(snapshot['playbacks'], {})
        self.app.reply_watch.start.assert_not_called()
        self.assertEqual(self.received('Dog Data'), b'')
        ready = {}
        self.provider = lambda context, specs, execute, **kwargs: ready.update(execute('list_ready', {})) or final()
        listed = self.app.jarvis.submit('What is ready?', 'ready')
        self.assertEqual([item['job_id'] for item in ready['job_notices']], [job_id])
        self.assertIn('Dog Data observation completed', listed['text'])
        done, receipt = self.job_status(job_id, 'read-result')
        self.assertEqual((done['job_status'], done['output']), ('completed', 'Observer evidence'))
        self.assertEqual(receipt['text'], 'Observer evidence')
        self.assertEqual(done['job_subject'], self.targets['Dog Data'])
        again = {}
        self.provider = lambda context, specs, execute, **kwargs: again.update(execute('list_ready', {})) or final()
        self.app.jarvis.submit('Anything else ready?', 'ready-again')
        self.assertEqual(again['job_notices'], [])
        self.assertEqual(len(self.notices()), 1)

    def wait_for(self, condition, what):
        deadline = time.monotonic() + 3
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(.01)

    def test_reopened_store_keeps_quiet_job_lifecycle_with_captured_identity_and_consumed_notice(self):
        asked, entered, release = [], threading.Semaphore(0), threading.Event()
        def observe(socket, agent, question):
            asked.append(agent['window_id'])
            if agent['window_id'] == self.targets['Controller']['tab']:
                raise RuntimeError('Terminal disconnected')
            if agent['window_id'] == self.targets['Notes']['tab']:
                entered.release()
                release.wait(5)
            return {'text': 'Observer evidence', 'snapshot': {'observed_at': 'now'}}
        with patch('jarvis_tools.ask_session', side_effect=observe):
            self.queue('Dog Data', 'Observe Dog', 'dog')
            self.queue('Controller', 'Observe Controller', 'controller')
            dog_job, controller_job = self.job_ids()
            self.wait_status(dog_job, 'completed')
            self.wait_status(controller_job, 'failed')
            self.wait_for(lambda: len(self.notices()) == 2, 'two quiet notices')
            self.queue('Notes', 'Observe Notes', 'notes')
            self.assertTrue(entered.acquire(timeout=1))
            notes_job = self.job_ids()[-1]
            self.provider = lambda context, specs, execute, **kwargs: execute('resolve_subject', {'name': 'Controller'}) and final()
            self.app.jarvis.submit('Switch to Controller', 'foreground')
            before, previous = self.app.jarvis.snapshot(), self.app
            self.reopen()
            release.set()
            self.wait_for(lambda: 'job_completion:' + notes_job in previous.jarvis.snapshot()['persistence_errors'], 'orphan worker diagnostic')
            after = self.app.jarvis.snapshot()
            self.assertEqual({key: job['status'] for key, job in after['jobs'].items()},
                             {dog_job: 'completed', controller_job: 'failed', notes_job: 'interrupted'})
            self.assertEqual([after['jobs'][key]['subject'] for key in (dog_job, controller_job, notes_job)],
                             [self.targets[name] for name in ('Dog Data', 'Controller', 'Notes')])
            self.assertEqual(after['jobs'][controller_job]['result']['error'], 'Terminal disconnected')
            self.assertEqual(after['subject'], self.targets['Controller'])
            self.assertEqual(set(after['turns']), set(before['turns']))
            self.assertEqual((after['playbacks'], after['persistence_errors']), ({}, {}))
            self.assertEqual(set(after['notices']), {'job:' + dog_job, 'job:' + controller_job})
            self.assertTrue(all(n['quiet'] and not n.get('consumed') for n in after['notices'].values()))
            self.assertEqual(after['notices']['job:' + controller_job]['status'], 'failed')
            self.app.reply_watch.start.assert_not_called()
            self.assertEqual(self.received('Notes'), b'')
            interrupted, receipt = self.job_status(notes_job, 'interrupted-after-reopen')
            self.assertEqual(interrupted['job_status'], 'interrupted')
            self.assertIn('not restarted', receipt['text'])
            read, _ = self.job_status(dog_job, 'read-dog')
            self.assertEqual((read['job_status'], read['output']), ('completed', 'Observer evidence'))
            consumed = self.app.jarvis.snapshot()['notices']['job:' + dog_job]
            self.assertTrue(consumed['consumed'] and isinstance(consumed['consumed_at'], float))
            self.reopen()
            kept = self.app.jarvis.snapshot()['notices']
            self.assertEqual(kept['job:' + dog_job], consumed)
            self.assertFalse(kept['job:' + controller_job].get('consumed'))
            ready = {}
            self.provider = lambda context, specs, execute, **kwargs: ready.update(execute('list_ready', {})) or final()
            self.app.jarvis.submit('What is ready after restart?', 'ready-after-reopen')
            self.assertEqual([item['job_id'] for item in ready['job_notices']], [controller_job])
            self.assertEqual(len(asked), 3)
            self.assertEqual(self.app.jarvis.snapshot()['subject'], self.targets['Controller'])

    def test_completion_store_failure_is_visible_never_durable_and_never_replays_the_worker(self):
        gate, slow = self.gated()
        with patch('jarvis_tools.ask_session', side_effect=slow) as asked:
            self.queue()
            self.assertTrue(gate['entered'].acquire(timeout=1))
            (job_id,) = self.job_ids()
            with patch.object(self.app.jarvis_store, 'checkpoint', side_effect=RuntimeError('disk full')):
                gate['release'].set()
                key = 'job_completion:' + job_id
                self.wait_for(lambda: key in self.app.jarvis.snapshot()['persistence_errors'], 'completion diagnostic')
                snapshot = self.app.jarvis.snapshot()
                disk = JarvisStore(self.root / 'jarvis.sqlite3')
                durable = disk.load()
                disk.close()
            diagnostic = snapshot['persistence_errors'][key]
            self.assertEqual((diagnostic['kind'], diagnostic['record_id'], diagnostic['persisted']), ('job_completion', job_id, False))
            self.assertIn('disk full', diagnostic['error'])
            self.assertEqual(snapshot['notices'], {})
            self.assertEqual(durable['jobs'][job_id]['status'], 'running')
            self.assertEqual((durable['notices'], durable['persistence_errors']), ({}, {}))
            status, receipt = self.job_status(job_id, 'after-failed-completion')
            self.assertEqual([item['record_id'] for item in status['persistence_errors']], [job_id])
            self.assertIn('Persistence problem recorded', receipt['text'])
            self.assertIn('disk full', receipt['text'])
            ready = {}
            self.provider = lambda context, specs, execute, **kwargs: ready.update(execute('list_ready', {})) or final()
            listed = self.app.jarvis.submit('What is ready?', 'ready-after-failure')
            self.assertIn('not saved durably', listed['text'])
            self.assertEqual(ready['job_notices'], [])
            self.assertEqual(asked.call_count, 1)
            self.assertEqual(len(self.job_ids()), 1)
        self.assertEqual(self.received('Dog Data'), b'')
        self.assertEqual(self.app.jarvis.snapshot()['playbacks'], {})

    def test_notice_and_consumption_failures_keep_the_result_and_surface_diagnostics(self):
        gate, slow = self.gated()
        gate['release'].set()
        with patch('jarvis_tools.ask_session', side_effect=slow) as asked:
            with patch.object(self.app.jarvis.state, 'record_notice', side_effect=RuntimeError('notice store down')):
                self.queue()
                (job_id,) = self.job_ids()
                self.wait_for(lambda: 'job_notice:' + job_id in self.app.jarvis.snapshot()['persistence_errors'], 'notice diagnostic')
            self.assertEqual(self.app.jarvis.snapshot()['jobs'][job_id]['status'], 'completed')
            self.assertEqual(self.notices(), [])
            with patch.object(self.app.jarvis.state, 'consume_notice', side_effect=RuntimeError('consume store down')):
                status, receipt = self.job_status(job_id, 'read-without-notice')
            self.assertEqual(status['output'].split(' Persistence')[0], 'Observer evidence')
            self.assertEqual({item['kind'] for item in status['persistence_errors']}, {'job_notice', 'job_notice_consume'})
            self.assertIn('notice store down', receipt['text'])
            self.assertEqual(asked.call_count, 1)

    def test_saturation_is_bounded_with_busy_receipt_and_new_foreground_stays_responsive(self):
        gate, slow = self.gated()
        with patch('jarvis_tools.ask_session', side_effect=slow):
            self.queue(request_id='one')
            self.queue('Controller', 'Observe Controller', 'two')
            for _ in range(2):
                self.assertTrue(gate['entered'].acquire(timeout=1))
            third_results = []
            self.provider = lambda context, specs, execute, **kwargs: third_results.append(execute('observer_job', {'name': 'Notes'})) or final()
            started = time.monotonic()
            third = self.app.jarvis.submit('Observe Notes', 'three')
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual((third_results[0]['ok'], third_results[0]['status']), (False, 'blocked'))
            self.assertIn('Both observer jobs are busy', third['text'])
            self.assertEqual(len(self.job_ids()), 2)
            self.assertEqual(gate['calls'], 2)
            self.provider = lambda context, specs, execute, **kwargs: execute('list_sessions', {}) and final()
            self.assertEqual(self.app.jarvis.submit('List chats', 'foreground')['status'], 'completed')
            gate['release'].set()
            for job_id in self.job_ids():
                self.wait_status(job_id, 'completed')
            deadline = time.monotonic() + 2
            while len(self.notices()) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(self.notices()), 2)
            self.queue('Notes', 'Observe Notes again', 'four')
            self.assertEqual(len(self.job_ids()), 3)

    def test_late_completion_after_turn_cancellation_updates_only_its_own_job(self):
        gate, slow = self.gated()
        with patch('jarvis_tools.ask_session', side_effect=slow):
            queued = self.queue()
            self.assertTrue(gate['entered'].acquire(timeout=1))
            (job_id,) = self.job_ids()
            self.provider = lambda context, specs, execute, **kwargs: execute('resolve_subject', {'name': 'Notes'}) and final()
            self.app.jarvis.submit('Switch to Notes', 'notes')
            self.app.jarvis.cancel(queued['turn_id'])
            before = self.app.jarvis.snapshot()
            gate['release'].set()
            self.wait_status(job_id, 'completed')
        after = self.app.jarvis.snapshot()
        self.assertEqual(after['subject'], self.targets['Notes'])
        self.assertEqual(after['turns'][queued['turn_id']]['status'], before['turns'][queued['turn_id']]['status'])
        self.assertEqual(after['active_turn_id'], before['active_turn_id'])
        self.assertEqual(after['jobs'][job_id]['subject'], self.targets['Dog Data'])
        self.assertEqual(after['jobs'][job_id]['turn_id'], queued['turn_id'])
        self.assertEqual(after['playbacks'], {})

    def test_failed_disconnected_job_has_receipt_frees_slot_and_notice_says_failed(self):
        with patch('jarvis_tools.ask_session', side_effect=RuntimeError('Terminal disconnected')):
            self.queue()
            (job_id,) = self.job_ids()
            self.wait_status(job_id, 'failed')
        job = self.app.jarvis.snapshot()['jobs'][job_id]
        self.assertEqual((job['result']['ok'], job['result']['status'], job['result']['error']), (False, 'failed', 'Terminal disconnected'))
        deadline = time.monotonic() + 2
        while not self.notices() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.notices()[0]['status'], 'failed')
        result, receipt = self.job_status(job_id, 'failed-status')
        self.assertEqual(result['job_status'], 'failed')
        self.assertIn('failed: Terminal disconnected', receipt['text'])
        self.assertNotIn('No message was submitted', receipt['text'])
        gate, slow = self.gated()
        with patch('jarvis_tools.ask_session', side_effect=slow) as asked:
            self.queue('Controller', 'Observe Controller', 'after-failure')
            self.assertTrue(gate['entered'].acquire(timeout=1))
            gate['release'].set()
        self.assertEqual(asked.call_count, 1)

    def test_interrupted_job_is_reported_unknown_and_never_restarted(self):
        with patch('jarvis_tools.ask_session') as asked:
            self.app.jarvis.state.record_job('turn', {'ok': True, 'status': 'running', 'job_id': 'restored', 'output': 'queued'},
                                             subject=self.targets['Dog Data'])
            self.app.jarvis.state.update_job('restored', 'interrupted', {'ok': False, 'status': 'interrupted', 'job_id': 'restored'})
            result, receipt = self.job_status('restored', 'interrupted-status')
            self.assertEqual(result['job_status'], 'interrupted')
            self.assertIn('result is unknown', receipt['text'])
            self.assertIn('not restarted', receipt['text'])
            self.provider = lambda *args, **kwargs: final('No tools.')
            self.app.jarvis.submit('Anything new?', 'new')
            asked.assert_not_called()
        self.assertEqual(self.notices(), [])

    def test_duplicate_completion_records_one_notice_with_the_same_id(self):
        gate, slow = self.gated()
        gate['release'].set()
        with patch('jarvis_tools.ask_session', side_effect=slow):
            self.queue()
            (job_id,) = self.job_ids()
            self.wait_status(job_id, 'completed')
            deadline = time.monotonic() + 2
            while not self.notices() and time.monotonic() < deadline:
                time.sleep(.01)
            tools = self.app.jarvis_tools
            tools.jobs_limit.acquire()
            tools.run_job(job_id, tools.agent(self.targets['Dog Data']), 'again')
        self.assertEqual([notice['id'] for notice in self.notices()], ['job:' + job_id])
        self.assertEqual(self.app.jarvis.snapshot()['jobs'][job_id]['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
