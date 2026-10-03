import json
import unittest

from jarvis_state import JarvisState, encoded_size


DOG = {'tab': '@dog', 'pane': '%dog', 'identity': 'dog:original', 'label': 'Dog Data'}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = JarvisState()
        self.turn, _ = self.state.begin_turn('Tell Dog: café\n x = 1;  ', 'first')
        self.id = self.turn['turn_id']
        self.state.set_subject(self.id, DOG)

    def test_literal_span_and_snapshot_are_exact_and_detached(self):
        draft = self.state.prepare_literal(self.id, 10, len(self.turn['user_text']))
        self.assertEqual(draft['text'], 'café\n x = 1;  ')
        self.assertEqual(draft['start'], 10)
        self.assertEqual(draft['source_turn_id'], self.id)
        draft['target']['identity'] = 'changed'
        snapshot = self.state.snapshot()
        snapshot['drafts'][draft['draft_id']]['text'] = 'changed'
        self.assertEqual(self.state.snapshot()['drafts'][draft['draft_id']]['text'], 'café\n x = 1;  ')
        self.assertEqual(self.state.snapshot()['drafts'][draft['draft_id']]['target'], DOG)

    def test_spans_require_real_codepoint_integers(self):
        for start, end in ((True, 4), (-1, 5), (0, 999), (2, 2), (4.0, 7)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.state.prepare_literal(self.id, start, end)

    def test_replacement_preserves_original_and_cannot_replace_submitted(self):
        original = self.state.prepare_literal(self.id, 10, 14)
        replacement = self.state.prepare_literal(self.id, 15, 20, supersedes=original['draft_id'])
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['drafts'][original['draft_id']]['text'], 'café')
        self.assertEqual(snapshot['drafts'][original['draft_id']]['status'], 'superseded')
        action = self.state.begin_action(self.id, 'send_draft', {}, replacement['draft_id'])
        self.state.finish_action(action['action_id'], {'ok': True, 'status': 'submitted'})
        with self.assertRaises(ValueError):
            self.state.prepare_literal(self.id, 10, 14, supersedes=replacement['draft_id'])
        with self.assertRaises(ValueError):
            self.state.finish_action(action['action_id'], {'ok': True, 'status': 'completed'})

    def test_send_receipt_cannot_fabricate_task_completion_or_retry_uncertain(self):
        draft = self.state.prepare_literal(self.id, 10, 14)
        action = self.state.begin_action(self.id, 'send_draft', {}, draft['draft_id'])
        with self.assertRaises(ValueError):
            self.state.finish_action(action['action_id'], {'ok': True, 'status': 'completed'})
        self.state.finish_action(action['action_id'], {'ok': False, 'status': 'uncertain'})
        with self.assertRaises(ValueError):
            self.state.begin_action(self.id, 'send_draft', {}, draft['draft_id'])

    def test_compaction_pins_subject_pending_work_and_exact_records(self):
        draft = self.state.prepare_literal(self.id, 10, 14)
        self.state.clarifications['clarify'] = {'question': 'Which dataset?', 'draft_id': draft['draft_id']}
        action = self.state.begin_action(self.id, 'send_draft', {}, draft['draft_id'])
        self.state.finish_action(action['action_id'], {'ok': False, 'status': 'uncertain'})
        self.state.record_job(self.id, {'ok': True, 'status': 'running', 'job_id': 'job-dog'})
        self.state.finish_turn(self.id, {'text': 'Queued.'})
        for i in range(40):
            turn, _ = self.state.begin_turn('Unrelated ' + 'x' * 2000, str(i))
            self.state.finish_turn(turn['turn_id'], {'text': 'y' * 1000})
        turn, _ = self.state.begin_turn('And the dog?', 'last')
        context = self.state.context(turn['turn_id'])
        self.assertEqual(context['subject'], DOG)
        self.assertEqual(context['jobs'][0]['job_id'], 'job-dog')
        self.assertEqual(context['actions'][0]['status'], 'uncertain')
        self.assertEqual(context['clarifications']['clarify']['draft_id'], draft['draft_id'])
        self.assertLessEqual(len(context['recent_turns']), 24)
        self.assertLessEqual(encoded_size(context), 65536)
        self.assertLessEqual(encoded_size(context['recent_turns']), 32768)
        self.assertEqual(self.state.snapshot()['drafts'][draft['draft_id']]['text'], 'café')
        json.dumps(context)

    def test_oversized_pinned_input_fails_visibly_without_truncation(self):
        turn, _ = self.state.begin_turn('x' * 70000, 'big')
        with self.assertRaisesRegex(ValueError, 'Pinned'):
            self.state.context(turn['turn_id'])
        self.assertEqual(len(self.state.snapshot()['turns'][turn['turn_id']]['user_text']), 70000)

    def test_cancelled_turn_invalidates_unsubmitted_draft_only(self):
        draft = self.state.prepare_literal(self.id, 10, 14)
        self.state.cancel(self.id)
        self.assertEqual(self.state.snapshot()['drafts'][draft['draft_id']]['status'], 'cancelled')
        with self.assertRaises(InterruptedError):
            self.state.set_subject(self.id, DOG)

    def test_playback_progress_requires_owner_and_survives_cancel_without_late_completion(self):
        self.state.finish_turn(self.id, {'text': 'Read this.'})
        playback = self.state.create_playback(self.id, 'phone', 7, 'One. Two.', {'id': 'reply'})
        self.assertEqual(playback['status'], 'generated')
        with self.assertRaises(ValueError):
            self.state.update_playback(playback['playback_id'], 'other', 7, 'completed')
        self.state.update_playback(playback['playback_id'], 'phone', 7, 'playing', ['phrase1'])
        self.state.update_playback(playback['playback_id'], 'phone', 7, 'cancelled', ['phrase1'])
        with self.assertRaises(ValueError):
            self.state.update_playback(playback['playback_id'], 'phone', 7, 'completed', ['phrase1', 'phrase2'])
        stored = self.state.snapshot()['playbacks'][playback['playback_id']]
        self.assertEqual(stored['completed_phrases'], ['phrase1'])
        self.assertEqual(stored['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
