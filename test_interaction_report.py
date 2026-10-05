import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from interaction_report import Interactions, main, read_events, readable


FIXTURE = Path(__file__).parent / 'fixtures' / 'interaction-events.json'


def record(line, stage, **fields):
    return {'source': {'path': 'synthetic.jsonl', 'line': line},
            'event': {'timestamp': '2026-10-05T20:00:00Z', 'stage': stage, **fields}}


def anchors(request='request-A', session='session-A', turn='turn-A', revision=2):
    return [record(1, 'http_turn_input', session_id=session, request_id=request, client_id='client-A',
                   transcript_id='transcript-A', transcript_revision=revision, text='Tell Dog Data: fixture.'),
            record(2, 'browser', client={'stage': 'turn_post', 'context': {'session_id': session},
                   'request_id': request, 'client_id': 'client-A', 'transcript_id': 'transcript-A',
                   'capture_id': 'capture-A', 'generation': 2}),
            record(3, 'canonical_receipt', request_id=request, turn_id=turn,
                   receipt={'session_id': session, 'request_id': request, 'turn_id': turn, 'status': 'completed',
                   'text': 'Input submitted. Task unconfirmed.', 'actions': [{'status': 'submitted', 'payload': 'fixture.'}]})]


class RealSchemaTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads(FIXTURE.read_text())
        self.events = [{'source': {'path': str(FIXTURE), 'line': number}, 'event': event}
                       for number, event in enumerate(fixture, 1)]
        self.interactions = Interactions(self.events)

    def test_actual_99_events_keep_two_requests_separate_in_one_capture(self):
        self.assertEqual(len(self.events), 99)
        self.assertEqual(len(self.interactions.recent()), 2)
        first = self.interactions.select('a38431b7')
        second = self.interactions.select(next(row['request_id'] for row in self.interactions.recent()
                                              if row['request_id'] != first['request_id']))
        first_lines = {row['source']['line'] for row in first['timeline']}
        second_lines = {row['source']['line'] for row in second['timeline']}
        self.assertFalse(first_lines & second_lines)
        self.assertEqual([row['text'] for row in first['evidence']['accepted_text']],
                         ['Tell Dog Data: fixture.', 'Tell Dog Data: fixture.'])
        self.assertEqual(len(first['evidence']['delivery']), 1)
        self.assertEqual(first['evidence']['delivery'][0]['status'], 'submitted')
        self.assertFalse(second['evidence']['delivery'])
        self.assertIn('Controller turn outcome', first['evidence']['controller_results'][0]['meaning'])
        for report, generation in ((first, '2'), (second, '3')):
            tts = [row for row in report['timeline'] if row['event'].get('kind') == 'tts_text_chunk']
            self.assertTrue(tts)
            self.assertEqual({row['event']['generation'] for row in tts}, {generation})
            self.assertTrue(any(row['event'].get('kind') == 'committed_transcript' for row in report['shared_capture_context']))
            self.assertTrue(any('shared capture' in gap for gap in report['gaps']))
        self.assertTrue(first['unassigned_sources'])
        text = readable(first)
        self.assertIn('Delivery:', text)
        self.assertIn('Selected timeline', text)
        self.assertNotIn('"tools":', text)
        self.assertIn('not assigned', text)

    def test_rendition_is_joined_by_playback_not_original_text(self):
        report = self.interactions.select(self.interactions.recent()[0]['request_id'])
        self.assertEqual(len(report['evidence']['renditions']), 1)
        self.assertIn('samples passed', report['evidence']['renditions'][0]['result']['text'])
        self.assertTrue(report['evidence']['originals'])
        self.assertTrue(any(item['full_acknowledgment_recorded'] for item in report['evidence']['playback']))
        self.assertTrue(all('not independently measured' in item['meaning'] for item in report['evidence']['playback']))


class IsolationTests(unittest.TestCase):
    def selected(self, extra):
        return Interactions(anchors() + extra).select('request-A')

    def test_revisionless_post_does_not_authorize_different_revision(self):
        report = self.selected([record(4, 'turn_decision_input', session_id='session-A', client_id='client-A',
                                       transcript_id='transcript-A', transcript_revision=3)])
        self.assertNotIn(4, [row['source']['line'] for row in report['timeline']])

    def test_foreign_session_or_client_and_next_dispatch_are_excluded(self):
        report = self.selected([
            record(4, 'current_observation', session_id='session-B', turn_id='turn-A'),
            record(5, 'browser', client={'stage': 'audio_ended', 'capture_id': 'capture-A', 'generation': 2,
                                        'client_id': 'client-B', 'context': {'session_id': 'session-B'}}),
            record(6, 'browser', client={'stage': 'transcript_dispatch', 'capture_id': 'capture-A', 'generation': 2,
                                        'transcript_id': 'transcript-B', 'transcript_revision': 2})])
        self.assertEqual([row['source']['line'] for row in report['timeline']], [1, 2, 3])
        self.assertEqual(len(report['unassigned_sources']), 3)

    def test_historical_ids_are_data_and_conflicting_supplement_does_not_pollute_anchor(self):
        extra = [record(4, 'provider_input', request_id='request-B', turn_id='turn-B',
                        context={'request_id': 'request-A', 'turn_id': 'turn-A', 'capture_id': 'capture-A', 'generation': 2}),
                 record(5, 'tool_result', request_id='request-B', turn_id='turn-A')]
        report = self.selected(extra)
        self.assertEqual([row['source']['line'] for row in report['timeline']], [1, 2, 3])
        self.assertEqual(report['ambiguous_sources'], [{'path': 'synthetic.jsonl', 'line': 5}])

    def test_reused_request_or_prefix_requires_explicit_session_and_exact_id_wins(self):
        events = anchors() + anchors(session='session-B', turn='turn-B') + [record(7, 'http_turn_input', request_id='request-A-more', session_id='session-A')]
        interactions = Interactions(events)
        with self.assertRaises(ValueError):
            interactions.select('request-A')
        with self.assertRaises(ValueError):
            interactions.select('request-', 'session-A')
        self.assertEqual(interactions.select('request-A', 'session-A')['request_id'], 'request-A')

    def test_ambiguous_transcript_and_audio_keys_are_not_guessed(self):
        events = anchors() + anchors(request='request-B', turn='turn-B') + [
            record(4, 'browser', client={'stage': 'transcript_dispatch', 'transcript_id': 'transcript-A', 'transcript_revision': 2}),
            record(5, 'voice_provider', kind='tts_text_chunk', capture_id='capture-A', generation='2')]
        report = Interactions(events).select('request-A')
        self.assertNotIn(4, [row['source']['line'] for row in report['timeline']])
        self.assertNotIn(5, [row['source']['line'] for row in report['timeline']])
        self.assertTrue(report['ambiguous_sources'])

    def test_timestamp_truncation_and_missing_playback_do_not_create_completion(self):
        report = self.selected([record(4, 'tool_result', request_id='request-A', timestamp=None),
                                record(5, 'provider_result', request_id='request-A', truncated=True, preview='partial'),
                                record(6, 'voice_provider', kind='tts_audio_chunk', capture_id='capture-A', generation='2')])
        self.assertTrue(report['input_issues'])
        self.assertTrue(any('truncated' in gap for gap in report['gaps']))
        self.assertFalse(report['evidence']['playback'])
        self.assertIn('not prove', ' '.join(report['gaps']))

    def test_conflicting_envelope_ids_do_not_override_request_receipt(self):
        report = self.selected([record(4, 'http_turn_receipt', request_id='request-A',
                                       result={'request_id': 'request-B', 'turn_id': 'turn-A'})])
        self.assertNotIn(4, [row['source']['line'] for row in report['timeline']])
        self.assertTrue(any('Conflicting' in issue['problem'] for issue in report['input_issues']))
        self.assertEqual(report['evidence']['controller_results'][0]['status'], 'completed')

    def test_full_playback_requires_application_boundary_acknowledgment(self):
        report = self.selected([record(4, 'playback_ledger', request={'turn_id': 'turn-A'},
            result={'turn_id': 'turn-A', 'playback_id': 'p1', 'status': 'completed',
                    'full_boundary_id': 'full', 'completed_phrases': ['full']}),
            record(5, 'playback_ledger', request={'playback_id': 'p1'},
                   result={'playback_id': 'p1', 'status': 'completed', 'completed_phrases': []})])
        self.assertEqual([item['full_acknowledgment_recorded'] for item in report['evidence']['playback']], [True, False])
        self.assertIn('not independently measured', report['evidence']['playback'][0]['meaning'])


class ReaderAndCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.log = self.root / 'pipeline.jsonl'

    def test_rotation_malformed_incomplete_and_bounds_preserve_locators(self):
        self.log.with_suffix('.jsonl.1').write_text(json.dumps(anchors()[0]['event']) + '\n')
        self.log.write_bytes(b'not json\n' + json.dumps(anchors()[2]['event']).encode() + b'\n' + b'{"stage":"partial"')
        events, issues = read_events(self.log)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]['source']['line'], 1)
        self.assertTrue(events[0]['source']['path'].endswith('.1'))
        self.assertEqual(events[1]['source']['line'], 2)
        self.assertEqual(len(issues), 2)
        self.log.write_text('not json\n' * 100)
        events, issues = read_events(self.log, max_events=3)
        self.assertLessEqual(len(issues), 4)
        self.assertTrue(any('limit' in issue['problem'] for issue in issues))

    def test_empty_probe_and_missing_log_have_no_user_interactions(self):
        events, issues = read_events(self.log)
        self.assertFalse(Interactions(events, issues).recent())
        self.assertTrue(issues)
        self.log.write_text(json.dumps({'stage': 'deployment_probe', 'timestamp': '2026-10-05T20:00:00Z'}) + '\n')
        events, issues = read_events(self.log)
        self.assertFalse(Interactions(events, issues).recent())

    def test_cli_readable_json_private_export_and_no_overwrite(self):
        self.log.write_text(''.join(json.dumps(item['event']) + '\n' for item in anchors()))
        output = self.root / 'reports' / 'request.json'
        main(['--log', str(self.log), '--request', 'request-A', '--json', '--output', str(output)])
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(output.parent).st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(output.read_text())['request_id'], 'request-A')
        original = output.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--log', str(self.log), '--output', str(output)])
        self.assertEqual(output.read_bytes(), original)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            main(['--log', str(self.log)])
        self.assertIn('request-A', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
