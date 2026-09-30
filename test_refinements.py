import unittest
from unittest.mock import patch

import jev
from message_delivery import prepare_message
from session_questions import terminal_content
import test_session_actions


class RefinementTests(unittest.TestCase):
    def test_terminal_ui_is_excluded_without_removing_real_messages(self):
        text = "› Reply with CORRECTION_OK\n• CORRECTION_OK\n› Ask Codex to do anything\n  GPT-5.6-Luna low · /tmp/test · Reply with CORRECTION_OK"
        self.assertEqual(terminal_content(text), "› Reply with CORRECTION_OK\n• CORRECTION_OK")

    def test_live_progress_and_background_status_survive_filter(self):
        text = "• Working (1s • esc to interrupt)\n1 background terminal running\n• Checking XYZ"
        self.assertEqual(terminal_content(text), text)

    def test_verbatim_text_never_calls_normalizer(self):
        text = 'What does it think?\nKeep "Nova" and punctuation!'
        with patch("message_delivery.answer_question") as generate:
            result = prepare_message("Tell Luna: " + text, text, "verbatim", "Luna")
        self.assertEqual(result["text"], text)
        generate.assert_not_called()

    def test_indirect_text_records_source_and_generated_message(self):
        with patch("message_delivery.answer_question", return_value={"text": "What do you think?", "model": "test"}) as generate:
            result = prepare_message("Ask Luna what it thinks", "what it thinks", "indirect_question", "Luna")
        self.assertEqual(result["text"], "What do you think?")
        self.assertEqual(result["source_text"], "what it thinks")
        self.assertEqual(generate.call_args.args[1]["recipient"], "Luna")

    def test_unknown_form_fails_without_generation(self):
        with patch("message_delivery.answer_question") as generate:
            with self.assertRaises(ValueError):
                prepare_message("request", "text", "unknown", "Luna")
        generate.assert_not_called()

    def test_aliases_are_candidate_metadata_not_request_replacements(self):
        request = "Tell Luna: Jeff thinks it is blue"
        tabs = [{"id": "@0", "name": "JEV controller"}, {"id": "@1", "name": "Jeff"}]
        payload = jev.question(request, tabs, "@0")
        self.assertEqual(payload["state"]["request"], request)
        self.assertIn("Jiv", payload["questions"]["target"]["criteria"]["@0"]["aliases"])
        self.assertNotIn("Jeff", payload["questions"]["target"]["criteria"]["@0"]["aliases"])
        self.assertEqual(payload["questions"]["target"]["criteria"]["@1"]["aliases"], [])


class DeliveryFailureTests(unittest.TestCase):
    setUp = test_session_actions.ActionDispatchTests.setUp
    register = test_session_actions.ActionDispatchTests.register

    def test_normalizer_failure_never_delivers_a_message(self):
        target = self.register()
        self.response = {"answers": {"action": {"choice": "send_message"}, "target": {"choice": target},
                                    f'pane:{target}': {'choice': '%1'},
                                    "message_start": {"choice": "9"}, "message_end": {"choice": "24"},
                                    "message_form": {"choice": "indirect_question"}}}
        with patch("server.prepare_message", side_effect=TimeoutError("normalizer unavailable")), \
             patch("server.send_message") as send:
            event = self.app.submit("Ask Beta what it thinks?")
        self.assertEqual(event["outcome"], "error")
        self.assertIn("normalizer unavailable", event["error"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
