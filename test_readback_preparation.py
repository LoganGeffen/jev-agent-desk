import threading
import unittest
from unittest.mock import Mock, patch

from reply_watch import ReadbackPreparation, history
from spoken_reply import ReadbackCache
import test_playground


def agent(id, provider='codex'):
    return {'provider': provider, 'thread_id': id, 'process': 'process-' + id,
            'socket': 'fixture', 'window_id': '@0', 'pane_id': '%0', 'run_dir': '/fixture'}


def turn(id, text, status='completed', phase='final_answer'):
    return {'id': id, 'status': status,
            'items': [{'type': 'agentMessage', 'phase': phase, 'text': text}]}


class PreparationTests(unittest.TestCase):
    def test_prepares_both_providers_without_delivery_and_only_when_complete(self):
        cache = Mock()
        watch = ReadbackPreparation(cache)
        agents = [agent('one'), agent('two', 'claude')]
        threads = {'one': {'turns': [turn('old', 'Old reply'), turn('new', 'Partial', 'inProgress')]},
                   'two': {'turns': [turn('other', 'Claude reply')]}}
        with patch('reply_watch.history', side_effect=lambda a: threads[a['thread_id']]):
            watch.poll(agents)
            self.assertEqual([c.args[0] for c in cache.prepare.call_args_list], ['Old reply', 'Claude reply'])
            watch.poll(agents)
            self.assertEqual(cache.prepare.call_count, 2)
            threads['one']['turns'][-1] = turn('new', 'Completed reply')
            watch.poll(agents)
            cache.prepare.assert_called_with('Completed reply')
            self.assertEqual(cache.prepare.call_count, 3)

    def test_skips_commentary_interrupted_and_invalid_replies(self):
        cache = Mock()
        watch = ReadbackPreparation(cache)
        for item in (turn('a', 'commentary', phase='commentary'), turn('a', 'partial', 'interrupted'),
                     turn('a', ' '), turn('a', 'x' * 20001)):
            with patch('reply_watch.history', return_value={'turns': [item]}):
                watch.poll([agent('one')])
        cache.prepare.assert_not_called()

    def test_identity_failure_does_not_prepare_stale_reply_or_block_other_sessions(self):
        cache = Mock()
        watch = ReadbackPreparation(cache)
        with patch('reply_watch.history', side_effect=[RuntimeError('identity changed'),
                {'turns': [turn('b', 'Safe reply')]}]):
            watch.poll([agent('one'), agent('two')])
        cache.prepare.assert_called_once_with('Safe reply')
        watch.poll([])
        self.assertEqual(watch.latest, {})

    def test_history_revalidates_after_read_for_both_providers(self):
        for provider in ('codex', 'claude'):
            with patch('reply_watch.' + provider + '_actions.check_target',
                       side_effect=[{}, RuntimeError('conversation changed')]) as validate, \
                 patch('reply_watch.' + provider + '_actions.thread_read',
                       return_value={'turns': [turn('a', 'Stale reply')]}):
                with self.assertRaisesRegex(RuntimeError, 'conversation changed'):
                    history(agent('one', provider))
                self.assertEqual(validate.call_count, 2)

    def test_worker_runs_without_browser_requests_and_stops(self):
        cache = Mock()
        prepared = threading.Event()
        cache.prepare.side_effect = lambda _: prepared.set()
        watch = ReadbackPreparation(cache)
        with patch('reply_watch.history', return_value={'turns': [turn('a', 'Reply')]}):
            watch.start(lambda: [agent('one')])
            self.assertTrue(prepared.wait(2))
            watch.stop()
            self.assertFalse(watch.worker.is_alive())

    def test_more_than_four_sessions_keep_their_latest_renditions(self):
        cache = ReadbackCache()
        self.addCleanup(cache.pool.shutdown)
        watch = ReadbackPreparation(cache)
        agents = [agent(str(i)) for i in range(8)]
        def render(text, mode, emit):
            emit(text)
            return {'text': text, 'mode': 'rendition'}
        with patch('reply_watch.history', side_effect=lambda a: {'turns': [turn('a', a['thread_id'])]}), \
             patch('spoken_reply.render_spoken_reply', side_effect=render) as generate:
            watch.poll(agents)
            for a in agents:
                self.assertEqual(cache.render(a['thread_id'], 'spoken')['text'], a['thread_id'])
            self.assertEqual(generate.call_count, 8)
            watch.poll(agents)
            self.assertEqual(generate.call_count, 8)
            watch.poll([])
            self.assertLessEqual(len(cache.entries), 4)

    def test_failed_background_render_is_not_repeated_by_polling(self):
        cache = ReadbackCache()
        self.addCleanup(cache.pool.shutdown)
        watch = ReadbackPreparation(cache)
        with patch('reply_watch.history', return_value={'turns': [turn('a', 'Reply')]}), \
             patch('spoken_reply.render_spoken_reply', side_effect=RuntimeError('failed')) as generate:
            watch.poll([agent('one')])
            cache.pool.shutdown()
            watch.poll([agent('one')])
            generate.assert_called_once()

    def test_server_discovers_all_bound_panes_without_jev_or_voice_configuration(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        def panes(window):
            return [{'id': window + '-1', 'provider': 'codex', 'thread_id': window,
                     'process': window, 'agent': True}, {'id': window + '-2', 'agent': False}]
        with patch.object(app, 'panes', side_effect=panes), patch.object(app, 'evaluate') as jev:
            agents = app.readback_agents()
        self.assertEqual(len(agents), len(fixture.tabs))
        self.assertEqual({a['window_id'] for a in agents}, {t['id'] for t in fixture.tabs})
        jev.assert_not_called()
