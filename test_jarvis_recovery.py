import tempfile
from pathlib import Path
import unittest

from jarvis_state import JarvisState, encoded_size
from jarvis_store import JarvisStore


class ContinuityTests(unittest.TestCase):
    def append(self, state, text, number):
        turn, _ = state.begin_turn(text, str(number))
        state.finish_turn(turn['turn_id'], {'text': 'Application response'})
        return turn['turn_id']

    def test_newest_correction_supersedes_exact_source_in_same_batch_and_restore(self):
        with tempfile.TemporaryDirectory() as path:
            store = JarvisStore(Path(path) / 'state.sqlite3')
            state = JarvisState(store=store)
            old = self.append(state, 'Prefer brief answers.', 0)
            newest = self.append(state, 'Correction: give detailed answers for scientific reports.', 1)
            for number in range(2, 32):
                self.append(state, 'Transient request ' + str(number), number)
            def classify(candidates, active):
                return list(reversed([{'source_turn_id': turn['turn_id'],
                    'category': 'preference' if turn['turn_id'] == old else 'correction' if turn['turn_id'] == newest else 'other',
                    **({'supersedes': old} if turn['turn_id'] == newest else {})} for turn in candidates]))
            state.compact_continuity(classify)
            self.assertEqual(state.decisions[old]['status'], 'superseded')
            store.close()
            store = JarvisStore(Path(path) / 'state.sqlite3')
            self.addCleanup(store.close)
            restored = JarvisState(store=store)
            turn, _ = restored.begin_turn('Which preference applies?', 'now')
            entries = restored.context(turn['turn_id'])['continuity']['entries']
            self.assertEqual([record['source_turn_id'] for record in entries], [newest])
            self.assertEqual(entries[0]['user_excerpt'], 'Correction: give detailed answers for scientific reports.')
            self.assertEqual(restored.read_user_source(old)['text'], 'Prefer brief answers.')
            self.assertEqual(restored.read_user_source(newest)['trust'], 'explicit_user_source')
            before = restored.snapshot()['decisions']
            for number in range(8):
                self.append(restored, 'Later transient request', 'later-' + str(number))
            restored.compact_continuity(lambda *args: (_ for _ in ()).throw(RuntimeError('Synthetic classifier failure')))
            self.assertEqual(restored.snapshot()['decisions'], before)
            self.assertEqual(restored.continuity['status'], 'failed')

    def test_archive_overflow_references_and_exact_unicode_pages(self):
        state = JarvisState()
        sources = []
        for number in range(65):
            text = ('Café 🐕. ' * 1400) if number == 0 else str(number) + ': standing preference ' + 'x' * 1800
            sources.append((self.append(state, text, number), text))
            state.compact_continuity(lambda candidates, active: [
                {'source_turn_id': turn['turn_id'], 'category': 'preference'} for turn in candidates])
        current, _ = state.begin_turn('Review remembered preferences', 'current')
        context = state.context(current['turn_id'])
        self.assertLessEqual(encoded_size(context), 65536)
        self.assertGreater(context['continuity']['archived_decision_count'], 0)
        refs = context['continuity']['omitted_source_refs']
        self.assertGreater(len(refs), 0)
        self.assertLessEqual(len(refs), 16)
        self.assertIn(context['continuity']['entries'][0]['source_turn_id'], [identifier for identifier, _ in sources[-26:]])
        identifier, original = sources[0]
        recovered, offset = '', 0
        while True:
            page = state.read_user_source(identifier, offset)
            self.assertLessEqual(encoded_size(page['text']), 8192)
            recovered += page['text']
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        self.assertEqual(recovered, original)
        for offset in (True, -1, len(original) + 1):
            with self.assertRaises(ValueError):
                state.read_user_source(identifier, offset)
        with self.assertRaises(ValueError):
            state.read_user_source('invented')

    def test_failed_or_forgotten_draft_requires_review_then_distinct_confirmation(self):
        from test_jarvis_state import DOG
        state = JarvisState()
        first, _ = state.begin_turn('exact Café 🐕. payload', 'first')
        state.set_subject(first['turn_id'], DOG)
        draft = state.prepare_literal(first['turn_id'], 0, len(first['user_text']))
        state.finish_turn(first['turn_id'], {'text': 'Provider failed'}, error='Synthetic failure')
        yes, _ = state.begin_turn('yes', 'yes')
        with self.assertRaises(ValueError):
            state.confirm_draft(yes['turn_id'], DOG, draft_id=draft['draft_id'])
        state.review_pending(yes['turn_id'], [draft['draft_id']])
        state.finish_turn(yes['turn_id'], {'text': 'Should I send this retained message?'})
        self.append(state, 'Unrelated question', 'unrelated')
        stale, _ = state.begin_turn('yes', 'stale')
        with self.assertRaises(ValueError):
            state.confirm_draft(stale['turn_id'], DOG, draft_id=draft['draft_id'])
        state.review_pending(stale['turn_id'], [draft['draft_id']])
        state.finish_turn(stale['turn_id'], {'text': 'Should I send this retained message?'})
        current, _ = state.begin_turn('Yes, send those exact retained words.', 'confirm')
        rebound = state.confirm_draft(current['turn_id'], DOG, draft_id=draft['draft_id'])
        self.assertEqual(rebound['text'], first['user_text'])
        self.assertEqual(rebound['source_turn_id'], first['turn_id'])
        self.assertEqual(rebound['turn_id'], current['turn_id'])
        self.assertEqual(rebound['target'], DOG)

    def test_352_unsent_drafts_remain_exact_without_context_exhaustion(self):
        from test_jarvis_state import DOG
        state = JarvisState()
        turn, _ = state.begin_turn('exact retained payload', 'prepare')
        state.set_subject(turn['turn_id'], DOG)
        drafts = [state.prepare_literal(turn['turn_id'], 0, len(turn['user_text'])) for _ in range(352)]
        self.assertLessEqual(encoded_size(state.context(turn['turn_id'])), 65536)
        self.assertEqual(sum(record['status'] == 'prepared' for record in state.drafts.values()), 1)
        state.discard_pending(turn['turn_id'], [record['draft_id'] for record in drafts])
        self.assertEqual(len(state.drafts), 352)
        self.assertTrue(all(record['status'] == 'discarded' and record['text'] == turn['user_text'] for record in state.drafts.values()))
        self.assertEqual(state.actions, {})

    def test_archive_tool_reads_source_without_calling_effect_handler(self):
        from jarvis_controller import JarvisController
        state = JarvisState()
        identifier = self.append(state, 'Prefer scientific detail. Café 🐕.', 'old')
        def provider(context, specs, execute, **kwargs):
            self.assertIn('read_user_source', [tool['name'] for tool in specs])
            record = execute('read_user_source', {'source_turn_id': identifier})
            self.assertEqual(record['trust'], 'explicit_user_source')
            return {'text': record['text']}
        core = JarvisController(provider, lambda *args: self.fail('Archive read must not dispatch an effect'), state=state)
        result = core.submit('Review archived preference', 'now')
        self.assertEqual(result['text'], 'Prefer scientific detail. Café 🐕.')
        self.assertEqual(state.actions, {})
