import unittest
from unittest.mock import patch

from spoken_reply import render_spoken_reply


class ReadbackTests(unittest.TestCase):
    def decision(self, choice):
        return {'model': 'fixture', 'answers': {'readback': {'choice': choice, 'confidence': .99}}}

    def test_original_is_exact_and_never_rewritten(self):
        text = 'Deployment is NOT verified.\nKeep the backup.'
        with patch('spoken_reply.evaluate', return_value=self.decision('original')) as judge, \
             patch('spoken_reply.answer_question') as rewrite:
            result = render_spoken_reply(text, 'auto')
        self.assertEqual(result['text'], text)
        self.assertEqual(result['mode'], 'original')
        self.assertEqual(judge.call_args.args[0]['state']['reply'], text)
        rewrite.assert_not_called()

    def test_structured_reply_uses_existing_complete_renderer(self):
        with patch('spoken_reply.evaluate', return_value=self.decision('rendition')), \
             patch('spoken_reply.answer_question', return_value={'text': 'One passed.', 'model': 'fixture'}) as rewrite:
            result = render_spoken_reply('| Passed | 1 |', 'auto')
        self.assertEqual(result['mode'], 'rendition')
        self.assertEqual(result['text'], 'One passed.')
        rewrite.assert_called_once()
        self.assertEqual(rewrite.call_args.args[1], {'original_reply': '| Passed | 1 |'})

    def test_uncertainty_never_rewrites_or_substitutes_text(self):
        with patch('spoken_reply.evaluate', return_value=self.decision('uncertain')), \
             patch('spoken_reply.answer_question') as rewrite:
            result = render_spoken_reply('Ambiguous', 'auto')
        self.assertEqual(result['mode'], 'uncertain')
        self.assertIsNone(result['text'])
        rewrite.assert_not_called()

    def test_explicit_modes_bypass_judgment(self):
        with patch('spoken_reply.evaluate') as judge, \
             patch('spoken_reply.answer_question', return_value={'text': 'Spoken'}) as rewrite:
            self.assertEqual(render_spoken_reply('Exact', 'original')['text'], 'Exact')
            self.assertEqual(render_spoken_reply('Exact', 'spoken')['text'], 'Spoken')
        judge.assert_not_called()
        rewrite.assert_called_once()

    def test_judge_failure_propagates_without_retry_or_rewrite(self):
        with patch('spoken_reply.evaluate', side_effect=RuntimeError('unavailable')) as judge, \
             patch('spoken_reply.answer_question') as rewrite:
            with self.assertRaises(RuntimeError):
                render_spoken_reply('Exact', 'auto')
        judge.assert_called_once()
        rewrite.assert_not_called()

    def test_invalid_input_never_calls_providers(self):
        with patch('spoken_reply.evaluate') as judge, patch('spoken_reply.answer_question') as rewrite:
            for text, mode in [('', 'auto'), ('x' * 20001, 'auto'), ('text', 'unknown')]:
                with self.assertRaises(ValueError):
                    render_spoken_reply(text, mode)
        judge.assert_not_called()
        rewrite.assert_not_called()


if __name__ == '__main__':
    unittest.main()
