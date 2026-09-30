import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import jev


class ProviderRecoveryTests(unittest.TestCase):
    def error(self, status=503):
        return HTTPError('https://api.typesafe.ai/v1/systemone', status, 'fixture', {}, io.BytesIO(b'private provider body'))

    def test_503_is_reported_without_retry(self):
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'fixture'}), \
                patch('jev.urlopen', side_effect=self.error()) as call:
            with self.assertRaisesRegex(RuntimeError, 'No action was taken') as caught:
                jev.evaluate({})
            self.assertNotIn('private provider body', str(caught.exception))
            self.assertEqual(call.call_count, 1)

    def test_auth_failure_is_not_retried(self):
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'fixture'}), \
                patch('jev.urlopen', side_effect=self.error(401)) as call:
            with self.assertRaises(jev.ProviderError):
                jev.evaluate({})
            self.assertEqual(call.call_count, 1)

    def test_inference_timeout_reports_no_action_without_retry(self):
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'fixture'}), \
                patch('jev.urlopen', side_effect=TimeoutError('timed out')) as call:
            with self.assertRaisesRegex(RuntimeError, 'No action was taken'):
                jev.evaluate({})
            self.assertEqual(call.call_count, 1)

    def test_provider_evidence_retains_body_and_request_id_but_redacts_key(self):
        error = HTTPError('https://api.typesafe.ai/v1/systemone', 503, 'fixture',
                          {'x-typesafe-request-id': 'req_test', 'Set-Cookie': 'excluded'},
                          io.BytesIO(b'{"error":"upstream unavailable", "echo":"fixture-secret"}'))
        with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'fixture-secret'}), \
                patch('jev.urlopen', side_effect=error):
            with self.assertRaises(jev.ProviderError) as caught:
                jev.evaluate({})
        details = caught.exception.provider_error
        self.assertEqual(details['headers'], {'x-typesafe-request-id': 'req_test'})
        self.assertIn('upstream unavailable', details['body'])
        self.assertNotIn('fixture-secret', details['body'])
