import json
from pathlib import Path
import tempfile
import unittest

from diagnostics import Diagnostics
from jarvis_controller import JarvisController


class DiagnosticTests(unittest.TestCase):
    def test_private_rotation_redaction_and_bounded_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'private' / 'pipeline.jsonl'
            trace = Diagnostics(path, max_bytes=600)
            for number in range(10):
                self.assertTrue(trace.record('input', request_id=str(number), text='exact source',
                    headers={'Authorization': 'do not retain'}, audio_base_64='do not retain',
                    nested={'api_key':'do not retain'}, note='Bearer fake-credential'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue(path.with_suffix('.jsonl.1').exists())
            self.assertLessEqual(path.stat().st_size, 600)
            self.assertNotIn('do not retain', path.read_text())
            self.assertNotIn('fake-credential', path.read_text())
            self.assertIn('exact source', path.read_text())
            self.assertTrue(trace.record('large', request_id='same-request', text='x' * 200000))
            self.assertTrue(json.loads(path.read_text().splitlines()[-1])['truncated'])
            self.assertEqual(json.loads(path.read_text().splitlines()[-1])['request_id'], 'same-request')
            self.assertLessEqual(path.stat().st_size,600)

    def test_logging_failure_cannot_change_controller_result(self):
        def failing(*args, **kwargs):
            raise OSError('disk unavailable')
        controller = JarvisController(lambda *args, **kwargs: {'text':'answer'}, lambda *args: {}, trace=failing)
        receipt = controller.submit('question','request')
        self.assertEqual(receipt['status'],'completed')
        self.assertEqual(receipt['text'],'answer')
        with tempfile.TemporaryDirectory() as temporary:
            blocker=Path(temporary)/'blocked'
            blocker.write_text('existing file')
            self.assertFalse(Diagnostics(blocker/'log').record('stage'))


if __name__ == '__main__':
    unittest.main()
