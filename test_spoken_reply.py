import unittest
from unittest.mock import patch

from spoken_reply import render_spoken_reply


class ReadbackTests(unittest.TestCase):
    def test_original_is_exact_and_never_rewritten(self):
        text = 'Deployment is NOT verified.\nKeep the backup.'
        chunks = []
        with patch('spoken_reply.answer_question') as rewrite:
            result = render_spoken_reply(text, 'original', chunks.append)
        self.assertEqual(result, {'text': text, 'mode': 'original'})
        self.assertEqual(chunks, [text])
        rewrite.assert_not_called()

    def test_auto_and_spoken_use_one_complete_renderer_without_jev(self):
        for mode in ('auto', 'spoken'):
            with patch('jev.evaluate') as judge, patch('spoken_reply.answer_question',
                    return_value={'text': 'One passed.', 'model': 'fixture'}) as rewrite:
                result = render_spoken_reply('| Passed | 1 |', mode)
            self.assertEqual(result['mode'], 'rendition')
            self.assertEqual(result['text'], 'One passed.')
            rewrite.assert_called_once()
            self.assertEqual(rewrite.call_args.args[1], {'original_reply': '| Passed | 1 |'})
            judge.assert_not_called()

    def test_generation_failure_propagates_without_retry(self):
        with patch('spoken_reply.answer_question', side_effect=RuntimeError('unavailable')) as rewrite:
            with self.assertRaises(RuntimeError):
                render_spoken_reply('Exact', 'auto')
        rewrite.assert_called_once()

    def test_invalid_input_never_calls_provider(self):
        with patch('spoken_reply.answer_question') as rewrite:
            for text, mode in [('', 'auto'), ('x' * 20001, 'auto'), ('text', 'unknown')]:
                with self.assertRaises(ValueError):
                    render_spoken_reply(text, mode)
        rewrite.assert_not_called()


class ListeningPreparationTests(unittest.TestCase):
    def test_stream_preparation_keeps_original_and_prompts_label_value_fidelity(self):
        from spoken_reply import ReadbackCache
        original = 'See [verification notes](/reports/2026-10-05/checks.md).\n3/5 passed; p = 0.05; deployment is NOT verified. Run `python check.py`.'
        rendition = 'See verification notes. Three of the five passed. P equals zero point zero five. Deployment is not verified. Run python check dot py.'
        requests = []
        def provider(question, evidence, instructions, on_text=None):
            requests.append((question, evidence, instructions))
            self.assertEqual(evidence['original_reply'], original)
            self.assertIn('speak the link label', instructions)
            self.assertIn('without rounding or changing', instructions)
            self.assertIn('substantive instruction or fact', instructions)
            for chunk in [rendition[:40], rendition[40:]]:
                on_text(chunk)
            return {'text': rendition, 'model': 'synthetic'}
        cache = ReadbackCache()
        self.addCleanup(cache.pool.shutdown)
        chunks = []
        with patch('spoken_reply.answer_question', side_effect=provider):
            result = cache.render(original, 'spoken', chunks.append)
        self.assertEqual(''.join(chunks), rendition)
        self.assertEqual(result['text'], rendition)
        self.assertNotIn('/reports/', result['text'])
        self.assertEqual(list(cache.entries), [(original, 'spoken')])
        original_chunks = []
        self.assertEqual(cache.render(original, 'original', original_chunks.append)['text'], original)
        self.assertEqual(original_chunks, [original])
        self.assertEqual(len(requests), 1)
