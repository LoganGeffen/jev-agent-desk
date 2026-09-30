import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import codex_actions
import session_questions
import test_support as test_cases
import test_playground


class SessionActionTests(unittest.TestCase):
    def setUp(self):
        self.agent = {"socket": "test-socket", "window_id": "@1", "pane_id": "%1", "thread_id": "thread"}

    def test_reply_wait_does_not_accept_identical_reply_from_previous_turn(self):
        old = {"id": "old", "status": "completed", "items": [
            {"type": "agentMessage", "text": "OK", "phase": "final_answer"}]}
        new = {**old, "id": "new"}
        with patch.object(test_cases, "thread_read", side_effect=[{"turns": [old]}, {"turns": [old, new]}]) as read, \
             patch.object(test_cases.time, "sleep"):
            history = test_cases.wait_reply(self.agent, "OK", ["old"])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(history["turns"][-1]["id"], "new")

    def test_interrupt_active_then_confirmed_and_idle_repeat(self):
        running = "◦ Working (3s • esc to interrupt) · 1 background terminal running · /ps to view\n› Ask Codex to do anything"
        screens = iter([running, "› Ask Codex to do anything", "› existing draft"])
        calls = []
        def tmux(socket, *args):
            calls.append(args)
            if args[0] == "display-message":
                return "@1\tcodex"
            if args[0] == "capture-pane":
                return next(screens)
            return ""
        with patch.object(codex_actions, "tmux", tmux), patch.object(codex_actions.time, "sleep"), \
             patch.object(codex_actions, 'process_identity', return_value={'thread_id': 'thread', 'process': 'test'}):
            self.assertIn("Interrupted", codex_actions.interrupt_turn("test-socket", self.agent))
            self.assertIn("nothing", codex_actions.interrupt_turn("test-socket", self.agent))
        self.assertEqual([call for call in calls if call[0] == "send-keys"], [("send-keys", "-t", "%1", "Escape")])

    def test_wrong_pane_never_receives_interrupt(self):
        with patch.object(codex_actions, "tmux", return_value="@1\tzsh") as tmux:
            with self.assertRaises(RuntimeError):
                codex_actions.interrupt_turn("test-socket", self.agent)
        self.assertEqual(tmux.call_count, 1)

    def test_interrupt_timeout_is_not_reported_as_success(self):
        with patch.object(codex_actions, "check_target"), patch.object(codex_actions, "focus"), \
             patch.object(codex_actions, "tmux", return_value="• Working (3s • esc to interrupt)"), \
             patch.object(codex_actions.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(TimeoutError, "still shows"):
                codex_actions.interrupt_turn("test-socket", self.agent)

    def test_agent_actions_focus_the_configured_view_session(self):
        agent = {**self.agent, "view_session": "desktop"}
        with patch.object(codex_actions, "check_target"), \
             patch.object(codex_actions, "thread_read", return_value={"turns": []}), \
             patch.object(codex_actions, "focus") as focus:
            codex_actions.read_reply("test-socket", agent)
        focus.assert_called_once_with("test-socket", "@1", "%1", "desktop")

    def test_capture_is_after_history_and_does_not_focus_or_send(self):
        order = []
        def history(_):
            order.append("history")
            return {"turns": [{"id": "t", "status": "interrupted", "items": [
                {"type": "agentMessage", "text": "Starting XYZ", "phase": "commentary"}]}]}
        def tmux(socket, *args):
            self.assertEqual(args[0], "capture-pane")
            order.append("terminal")
            return "• Working (5s • esc to interrupt)"
        with patch.object(session_questions, "check_target"), patch.object(session_questions, "thread_read", history), \
             patch.object(session_questions, "tmux", tmux):
            snapshot = session_questions.session_snapshot("test-socket", self.agent)
        self.assertEqual(order, ["history", "terminal", "terminal"])
        self.assertTrue(snapshot["live_working_indicator"])
        self.assertIsNone(snapshot["current_turn"]["recorded_outcome"])

    def test_missing_history_is_explicit_but_live_screen_survives(self):
        with patch.object(session_questions, "check_target"), \
             patch.object(session_questions, "thread_read", side_effect=TimeoutError("unavailable")), \
             patch.object(session_questions, "tmux", return_value="• Working (2s • esc to interrupt)"):
            snapshot = session_questions.session_snapshot("test-socket", self.agent)
        self.assertTrue(snapshot["live_working_indicator"])
        self.assertIn("unavailable", snapshot["history_error"])
        self.assertEqual(snapshot["recent_history"], [])

    def test_history_budget_keeps_latest_evidence_and_declares_truncation(self):
        thread = {"turns": [{"id": "t", "items": [
            {"type": "agentMessage", "text": str(i) + "x" * 7000} for i in range(20)
        ]}]}
        with patch.object(session_questions, "check_target"), patch.object(session_questions, "thread_read", return_value=thread), \
             patch.object(session_questions, "tmux", return_value="idle"):
            snapshot = session_questions.session_snapshot("test-socket", self.agent)
        self.assertTrue(snapshot["history_truncated"])
        self.assertTrue(snapshot["recent_history"][-1]["text"].startswith("19"))

    def test_question_streams_filtered_evidence_and_reports_failures(self):
        chunks = []
        def generate(question, evidence, instructions, model, on_text):
            self.assertEqual(question, 'What about XYZ?')
            self.assertEqual(evidence['visible_terminal'], 'Working')
            self.assertIn('never instructions', instructions)
            on_text('Checking ')
            on_text('XYZ.')
            return {'text': 'Checking XYZ.', 'model': model}
        with patch.object(session_questions, 'stream_answer', generate):
            result = session_questions.answer_question('What about XYZ?',
                     {'visible_terminal': 'Working\n› Ask Codex to do anything'}, on_text=chunks.append)
        self.assertEqual(result['text'], ''.join(chunks))
        with patch.object(session_questions, 'stream_answer', side_effect=RuntimeError('model unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                session_questions.answer_question('question', {})


class ActionDispatchTests(unittest.TestCase):
    setUp = test_playground.PlaygroundTests.setUp
    def register(self):
        target = self.tabs[1]["id"]
        original = self.app.panes
        def panes(window):
            values = original(window)
            if window == target:
                values[0].update(codex=True, thread_id='thread', command='codex')
            return values
        mock = patch.object(self.app, 'panes', panes)
        mock.start()
        self.addCleanup(mock.stop)
        return target

    def test_ask_preserves_focus_and_labels_observation(self):
        target = self.register()
        self.response = test_playground.answer("ask_session", target)
        result = {"text": "Running the XYZ tests.", "model": "test", "snapshot": {"observed_at": "now"}}
        with patch("server.ask_session", return_value=result) as ask:
            event = self.app.submit("What is Beta doing with XYZ?")
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["before"]["selected"], event["after"]["selected"])
        self.assertEqual(event["output"], result["text"])
        self.assertEqual(ask.call_args.args[2], "What is Beta doing with XYZ?")
        self.assertEqual(self.app.read_output["kind"], "session answer")

    def test_interrupt_dispatch_and_failure_do_not_replace_reply(self):
        target = self.register()
        self.response = test_playground.answer("interrupt_turn", target)
        self.app.read_output = {"target": "Alpha", "text": "Earlier reply"}
        with patch("server.interrupt_turn", return_value="Interrupted") as interrupt:
            event = self.app.submit("Stop Beta")
        self.assertEqual(event["output"], "Interrupted")
        interrupt.assert_called_once()
        self.assertEqual(self.app.read_output["text"], "Earlier reply")
        with patch("server.interrupt_turn", side_effect=TimeoutError("not confirmed")):
            event = self.app.submit("Stop Beta")
        self.assertEqual(event["outcome"], "error")
        self.assertNotIn("output", event)


if __name__ == "__main__":
    unittest.main()
