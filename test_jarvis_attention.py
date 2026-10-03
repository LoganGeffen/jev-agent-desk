import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from attention import AttentionQueue
from jarvis_state import JarvisState
from jarvis_store import JarvisStore
from reply_watch import ReplyWatch
from test_attention import agent, judge, tab
from test_readback_preparation import turn


class DurableAttentionTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = []
        self.queue = AttentionQueue(on_change=self.snapshots.append)
        self.tabs = [tab('a'), tab('b')]

    def populate(self):
        self.queue.observe(agent('a'), {'turns': [turn('a1', 'All tests passed.')]})
        self.queue.observe(agent('b'), {'turns': [turn('b1', 'Which option should I use?')]})
        self.queue.classify(self.queue.view(self.tabs, include_heard=True), judge)

    def test_callback_runs_outside_the_queue_lock_and_snapshot_is_json_with_array_keys(self):
        held = []
        def callback(snapshot):
            held.append(self.queue.lock.locked())
            self.queue.view(self.tabs)
            json.dumps(snapshot)
        self.queue.on_change = callback
        self.populate()
        self.assertTrue(held and not any(held))
        snapshot = json.loads(json.dumps(self.queue.snapshot()))
        self.assertTrue(all(isinstance(entry['key'], list) and len(entry['key']) == 5 for entry in snapshot['entries']))
        self.assertEqual({entry['decision'] for entry in snapshot['entries']}, {'input', 'reply'})

    def test_restore_keeps_ids_and_only_durably_acknowledged_heard_state_without_replay(self):
        self.populate()
        items = self.queue.view(self.tabs)
        heard_id = next(item['id'] for item in items if item['tab'] == '@a')
        self.queue.heard(heard_id)
        saved = json.loads(json.dumps(self.snapshots[-1]))
        restored = AttentionQueue()
        restored.restore(saved)
        self.assertEqual(restored.view(self.tabs, include_heard=True), [])
        for name, reply in (('a', 'All tests passed.'), ('b', 'Which option should I use?')):
            restored.observe(agent(name), {'turns': [turn(name + '1', reply)]})
        after = {item['tab']: item for item in restored.view(self.tabs, include_heard=True)}
        self.assertEqual({key: value['id'] for key, value in after.items()}, {item['tab']: item['id'] for item in items})
        self.assertEqual([item['tab'] for item in restored.view(self.tabs)], ['@b'])
        self.assertEqual(after['@b']['decision'], 'input')
        restored.observe(agent('a'), {'turns': [turn('a2', 'A new reply.')]})
        self.assertIn('@a', [item['tab'] for item in restored.view(self.tabs)])

    def test_rereading_original_after_a_heard_rendition_keeps_actual_heard_state(self):
        self.populate()
        item = next(item for item in self.queue.view(self.tabs) if item['tab'] == '@a')
        self.queue.heard(item['id'])
        self.queue.view(self.tabs, include_heard=True)
        self.queue.observe(agent('a'), {'turns': [turn('a1', 'All tests passed.')]})
        self.queue.heard(item['id'])
        self.assertEqual([entry['tab'] for entry in self.queue.view(self.tabs)], ['@b'])
        self.assertEqual(next(entry for entry in self.queue.view(self.tabs, include_heard=True) if entry['tab'] == '@a')['id'], item['id'])

    def test_failed_durable_ack_leaves_reply_unheard_and_reports_the_failure(self):
        self.populate()
        item = next(item for item in self.queue.view(self.tabs) if item['tab'] == '@a')
        self.queue.on_change = lambda snapshot: (_ for _ in ()).throw(OSError('disk full'))
        with self.assertRaises(OSError):
            self.queue.heard(item['id'])
        self.assertIn(item['id'], [entry['id'] for entry in self.queue.view(self.tabs)])
        self.assertEqual(self.queue.persist_error, 'disk full')

    def test_background_persistence_failure_does_not_break_observation(self):
        self.queue.on_change = lambda snapshot: (_ for _ in ()).throw(OSError('disk full'))
        self.queue.observe(agent('a'), {'turns': [turn('a1', 'Reply.')]})
        self.assertEqual([item['tab'] for item in self.queue.view(self.tabs)], ['@a'])

    def test_concurrent_changes_end_with_the_latest_snapshot(self):
        self.queue.observe(agent('a'), {'turns': [turn('a1', 'Reply.')]})
        item = self.queue.view(self.tabs)[0]
        threads = [threading.Thread(target=self.queue.heard, args=(item['id'],)),
                   threading.Thread(target=self.queue.observe, args=(agent('b'), {'turns': [turn('b1', 'Other.')]}))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.snapshots[-1], self.queue.snapshot())

    def test_restore_rejects_invalid_keys(self):
        with self.assertRaises(ValueError):
            AttentionQueue().restore({'entries': [{'key': ['only', 'two'], 'id': 'x', 'reply': None, 'heard': False,
                                                   'arrived': 0, 'decision': None, 'answered': False}]})


class SlowFailure(AttentionQueue):
    def emit(self, strict=False):
        try:
            super().emit(strict)
        except Exception:
            time.sleep(.3)
            raise


class StoredAttentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='jarvis-attention-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.sqlite3'
        self.tabs = [tab('a'), tab('b')]
        self.open()
        self.queue.observe(agent('a'), {'turns': [turn('a1', 'All tests passed.')]})
        self.queue.observe(agent('b'), {'turns': [turn('b1', 'Which option should I use?')]})
        self.queue.classify(self.queue.view(self.tabs, include_heard=True), judge)

    def open(self):
        self.store = JarvisStore(self.path)
        self.addCleanup(self.store.close)
        self.state = JarvisState(store=self.store)
        self.queue = AttentionQueue()
        self.queue.restore(self.state.attention)
        self.queue.on_change = self.state.set_attention

    def reopen(self):
        self.store.close()
        self.open()
        for name, reply in (('a', 'All tests passed.'), ('b', 'Which option should I use?')):
            self.queue.observe(agent(name), {'turns': [turn(name + '1', reply)]})
        return {item['tab']: item for item in self.queue.view(self.tabs, include_heard=True)}

    def item(self, name):
        return next(item for item in self.queue.view(self.tabs) if item['tab'] == '@' + name)

    def test_acknowledgment_is_durable_only_after_the_real_checkpoint_commits(self):
        item = self.item('a')
        self.queue.heard(item['id'])
        self.assertEqual([entry['tab'] for entry in self.reopen().values() if entry['tab'] == '@a'], ['@a'])
        self.assertEqual([entry['tab'] for entry in self.queue.view(self.tabs)], ['@b'])

    def test_failed_acknowledgment_stays_unheard_across_later_checkpoints_and_reopen(self):
        item = self.item('a')
        with patch.object(self.store, 'checkpoint', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.queue.heard(item['id'])
        self.assertEqual(self.queue.persist_error, 'disk full')
        self.assertFalse(next(entry for entry in self.state.snapshot()['attention']['entries'] if entry['id'] == item['id'])['heard'])
        self.assertIn('@a', [entry['tab'] for entry in self.queue.view(self.tabs)])
        self.state.record_notice({'id': 'later', 'kind': 'job_result'})
        self.queue.observe(agent('b'), {'turns': [turn('b2', 'Newer reply.')]})
        restored = self.reopen()
        self.assertEqual(restored['@a']['id'], item['id'])
        self.assertIn('@a', [entry['tab'] for entry in self.queue.view(self.tabs)])

    def test_concurrent_checkpoint_during_a_failing_acknowledgment_cannot_commit_it(self):
        item, calls = self.item('a'), []
        self.queue.__class__ = SlowFailure
        def callback(snapshot):
            if not calls:
                calls.append(threading.Thread(target=self.queue.observe, args=(agent('b'), {'turns': [turn('b2', 'Newer reply.')]})))
                calls[0].start()
                time.sleep(.1)
                with patch.object(self.store, 'checkpoint', side_effect=RuntimeError('disk full')):
                    self.state.set_attention(snapshot)
            else:
                self.state.set_attention(snapshot)
        self.queue.on_change = callback
        with self.assertRaisesRegex(RuntimeError, 'disk full'):
            self.queue.heard(item['id'])
        calls[0].join(3)
        self.assertFalse(calls[0].is_alive())
        restored = self.reopen()
        self.assertIn('@a', [entry['tab'] for entry in self.queue.view(self.tabs)])
        self.assertEqual(restored['@a']['id'], item['id'])


class ReplyNoticeTests(unittest.TestCase):
    def test_reply_notice_identity_is_deterministic_and_never_duplicated(self):
        watch = ReplyWatch()
        target = {**agent('a'), 'socket': 'fixture'}
        thread = {'turns': [{'id': 'old', 'status': 'completed', 'items': []},
                            {'id': 'new', 'status': 'completed', 'items': [
                                {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'Hello'}]},
                                {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Answer.'}]}]}
        done = []
        inline = lambda target, daemon: type('Inline', (), {'start': staticmethod(target)})()
        for _ in range(2):
            with patch('reply_watch.history', return_value=thread), patch('reply_watch.threading.Thread', side_effect=inline):
                watch.start(target, {'old'}, 'Hello', 'A', 'request-1')
            done.append(watch.view())
        self.assertEqual(len(done[1]), 1)
        self.assertEqual(done[0][0]['id'], done[1][0]['id'])
        self.assertEqual(done[1][0]['turn_id'], 'new')


if __name__ == '__main__':
    unittest.main()
