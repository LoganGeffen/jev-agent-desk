import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jev
from codex_actions import latest_reply
from launch import fixtures
from server import Playground, activity_state, make_server, tmux


def answer(action, target="unused", pane=None):
    return {"model": "test-stub-not-jev", "answers": {
        "delivery": {"choice": "send"},
        "subject_scope": {"choice": "named"},
        "intent": {"choice": "routed_message" if action == "send_message" else "no_action" if action == "no_action" else "inspect" if action in ("read_reply", "ask_session") else "controller"},
        "action": {"type": "choice", "choice": action, "probabilities": {action: 0.4}},
        f"pane:{target}": {"type": "choice", "choice": pane or target.replace('@', '%')},
        "target": {"type": "choice", "choice": target, "probabilities": {target: 0.4}}}}


class PlaygroundTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="jev-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.socket = str(self.root / "tmux.sock")
        fixtures(self.socket)
        self.addCleanup(tmux, self.socket, "kill-server")
        self.response = answer("no_action")
        self.payloads = []

        def evaluate(payload):
            self.payloads.append(payload)
            return self.response

        self.app = Playground(self.socket, self.root / "events.jsonl", evaluate)
        self.tabs = self.app.observe()["tabs"]

    def test_selection_executes_low_confidence_choice_and_logs_full_input(self):
        self.response = answer("select_tab", self.tabs[1]["id"])
        event = self.app.submit("Alpha—actually, Beta")
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["after"]["selected"], self.tabs[1]["id"])
        self.assertEqual(len(self.payloads), 2)
        self.assertIn("questions", event["input"])
        self.assertEqual(event["input"]["state"]["request"], "Alpha—actually, Beta")
        self.assertEqual(event["response"]["model"], "test-stub-not-jev")
        self.assertEqual(json.loads(self.app.log.read_text()), event)

    def test_list_and_no_action_ignore_target_and_leave_selection(self):
        for action in ["list_tabs", "no_action"]:
            self.response = answer(action, "not-a-real-target")
            event = self.app.submit("a request")
            self.assertEqual(event["outcome"], "ok")
            self.assertEqual(event["before"]["selected"], event["after"]["selected"])
            self.assertIsNone(event["target"])
            self.assertEqual([tab['id'] for tab in event['after']['tabs']], [tab['id'] for tab in self.tabs])

    def test_tabs_expose_private_groups_and_activity_state(self):
        first = self.tabs[0]
        (self.root / "agents.json").write_text(json.dumps({
            first["id"]: {"socket": self.socket}
        }))
        tmux(self.socket, "set-option", "-w", "-t", first["id"], "@agent_status", "done")
        state = self.app.observe()
        tabs = state["tabs"]
        marked = next(tab for tab in tabs if tab["id"] == first["id"])
        self.assertEqual(marked["group"], "Workspace")
        self.assertEqual(marked["group_id"], "workspace")
        self.assertFalse(marked['codex'], 'Stale registration must not make a shell an agent')
        self.assertEqual(marked["state"], "ready")
        self.assertTrue(all(isinstance(tab["activity"], int) for tab in tabs))
        self.assertTrue(all(tab["group"] in {"Agents", "Workspace"} for tab in tabs))
        self.assertEqual(state["groups"], [
            {"id": "workspace", "name": "Workspace"},
            {"id": "agents", "name": "Agents"},
        ])

    def test_groups_create_rename_move_order_and_persist(self):
        state = self.app.create_group("Research")
        group = next(group for group in state["groups"] if group["name"] == "Research")
        self.assertFalse(any(tab["group_id"] == group["id"] for tab in state["tabs"]))

        state = self.app.rename_group(group["id"], "Review")
        self.assertIn({"id": group["id"], "name": "Review"}, state["groups"])
        first, second = self.tabs[:2]
        self.app.move_tab(first["id"], group["id"])
        state = self.app.move_tab(second["id"], group["id"], first["id"])
        self.assertEqual(
            [tab["id"] for tab in state["tabs"] if tab["group_id"] == group["id"]],
            [second["id"], first["id"]],
        )

        reloaded = Playground(self.socket, self.root / "events.jsonl", self.app.evaluate).observe()
        self.assertEqual(
            [tab["id"] for tab in reloaded["tabs"] if tab["group_id"] == group["id"]],
            [second["id"], first["id"]],
        )

    def test_create_and_rename_plain_terminal_tab(self):
        before = self.app.observe()
        state = self.app.create_tab("Scratch", "workspace")
        created = next(tab for tab in state["tabs"] if tab["id"] not in {tab["id"] for tab in before["tabs"]})
        self.assertEqual(state["selected"], created["id"])
        self.assertEqual(created["name"], "Scratch")
        self.assertEqual(created["group_id"], "workspace")

        state = self.app.rename_tab(created["id"], "Notes")
        self.assertEqual(next(tab for tab in state["tabs"] if tab["id"] == created["id"])["name"], "Notes")

    def test_terminal_text_escape_and_double_escape_reach_selected_pane(self):
        window = tmux(self.socket, "new-window", "-d", "-P", "-F", "#{window_id}",
                      "-t", "tabs:", "-n", "Input test", "cat -v")
        tmux(self.socket, "select-window", "-t", f"tabs:{window}")
        pane = self.app.panes(window)[0]
        binding = {'pane_id': pane['id'], 'identity': pane['identity']}
        self.app.terminal_input(window, text="DIRECT_INPUT_OK", **binding)
        self.app.terminal_input(window, key="Escape", **binding)
        self.app.terminal_input(window, key="Escape", **binding)
        deadline = time.monotonic() + 2
        screen = ""
        while time.monotonic() < deadline:
            screen = tmux(self.socket, "capture-pane", "-p", "-t", f"tabs:{window}")
            if "DIRECT_INPUT_OK^[^[" in screen:
                break
            time.sleep(.02)
        self.assertIn("DIRECT_INPUT_OK^[^[", screen)
        with self.assertRaisesRegex(ValueError, "Unsupported terminal key"):
            self.app.terminal_input(window, key="DefinitelyNotAKey")

    def test_no_conversation_history_and_fresh_selection(self):
        self.response = answer("select_tab", self.tabs[1]["id"])
        self.app.submit("first request")
        self.response = answer("no_action")
        self.app.submit("second request")
        state = self.payloads[2]["state"]
        self.assertEqual(state["selected_tab"], self.tabs[1]["id"])
        self.assertEqual(set(state), {"request", "tabs", "selected_tab"})
        self.assertNotIn("first request", json.dumps(self.payloads[2]))

    def test_latest_decision_survives_private_server_restart(self):
        event = self.app.submit("do nothing")
        reloaded = Playground(self.socket, self.root / "events.jsonl", self.app.evaluate)
        self.assertEqual(reloaded.state()["latest"], event)

    def test_target_not_in_supplied_tabs_cannot_select_another_session(self):
        tmux(self.socket, "new-session", "-d", "-s", "outside", "sleep 60")
        outside = tmux(self.socket, "display-message", "-p", "-t", "outside:", "#{window_id}")
        self.response = answer("select_tab", outside)
        event = self.app.submit("invalid target")
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["before"]["selected"], event["after"]["selected"])

    def test_non_tabs_grouped_view_is_scoped_and_focuses_only_that_view(self):
        tmux(self.socket, "new-session", "-d", "-s", "source", "-n", "One", "cat")
        first = tmux(self.socket, "display-message", "-p", "-t", "=source:", "#{window_id}")
        second = tmux(self.socket, "new-window", "-d", "-P", "-F", "#{window_id}",
                      "-t", "source:", "-n", "Two", "cat")
        tmux(self.socket, "new-session", "-d", "-t", "source", "-s", "desktop")
        tmux(self.socket, "new-session", "-d", "-t", "source", "-s", "phone")
        app = Playground(self.socket, self.root / "grouped-events.jsonl", self.app.evaluate,
                         source_session="source", view_session="desktop")

        state = app.select_tab(second)
        self.assertEqual(state["target"], {"source_session": "source", "view_session": "desktop"})
        self.assertEqual(state["selected"], second)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=desktop:", "#{window_id}"), second)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=source:", "#{window_id}"), first)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=phone:", "#{window_id}"), first)

        state = app.create_tab("Three", "workspace")
        created = state["selected"]
        expected = set(tmux(self.socket, "list-windows", "-t", "=source", "-F", "#{window_id}").splitlines())
        self.assertIn(created, expected)
        for session in ("desktop", "phone"):
            self.assertEqual(set(tmux(self.socket, "list-windows", "-t", f"={session}",
                                      "-F", "#{window_id}").splitlines()), expected)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=desktop:", "#{window_id}"), created)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=source:", "#{window_id}"), first)

    def test_unrelated_source_and_view_sessions_are_rejected(self):
        tmux(self.socket, "new-session", "-d", "-s", "outside", "cat")
        with self.assertRaisesRegex(ValueError, "do not share"):
            Playground(self.socket, self.root / "outside.jsonl", source_session="tabs",
                       view_session="outside")

    def test_attached_event_log_removes_terminal_and_observer_evidence(self):
        event = {
            "before": {"terminal": "private terminal", "terminal_ansi": "private ansi",
                       "tabs": [{"panes": [{"terminal_ansi": "private pane", "id": "%1"}]}]},
            "after": {"terminal": "after terminal", "tabs": []},
            "execution": {"observation": {"observed_at": "now", "thread_id": "thread",
                                                 "recent_history": ["private history"],
                                                 "visible_terminal": "private observer terminal"}},
            "output": "requested result",
        }
        stored = Playground.event_for_log(event)
        self.assertNotIn("terminal", stored["before"])
        self.assertNotIn("terminal_ansi", stored["before"]["tabs"][0]["panes"][0])
        self.assertEqual(stored["execution"]["observation"],
                         {"observed_at": "now", "thread_id": "thread"})
        self.assertEqual(stored["output"], "requested result")

    def test_provider_failure_is_error_not_no_action(self):
        def fail(_):
            raise TimeoutError("provider timeout")
        self.app.evaluate = fail
        event = self.app.submit("Go to Beta")
        self.assertEqual(event["outcome"], "error")
        self.assertIsNone(event["action"])
        self.assertIn("timeout", event["error"])
        self.assertEqual(event["before"]["selected"], event["after"]["selected"])
        self.assertEqual(len(self.app.log.read_text().splitlines()), 1)

    def test_missing_target_at_execution_is_logged_without_retry(self):
        def disappear(payload):
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                tmux(self.socket, "kill-window", "-t", self.tabs[1]["id"])
            return answer("select_tab", self.tabs[1]["id"])
        self.app.evaluate = disappear
        event = self.app.submit("Go to Beta")
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["action"], "select_tab")
        self.assertEqual(len(self.payloads), 2)

    def test_malformed_provider_result_is_logged(self):
        self.response = {"model": "test-stub-not-jev"}
        event = self.app.submit("Go to Beta")
        self.assertEqual(event["outcome"], "error")
        self.assertIn("KeyError", event["error"])

    def test_http_page_state_submission_and_origin_boundary(self):
        server = make_server(self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(url) as page:
            self.assertIn(b'Terminal panes', page.read())
        self.response = answer("select_tab", self.tabs[1]["id"])
        request = Request(url + "/api/request", data=b'{"request":"Go to Beta"}',
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            event = json.load(response)
        self.assertEqual(event["after"]["selected"], self.tabs[1]["id"])
        with urlopen(url + "/api/state") as response:
            state = json.load(response)
        self.assertEqual(state["latest"], event)
        self.assertEqual(state["selected"], self.tabs[1]["id"])
        direct = Request(url + "/api/select", data=json.dumps({"tab": self.tabs[0]["id"]}).encode(),
                         headers={"Content-Type": "application/json"})
        with urlopen(direct) as response:
            state = json.load(response)
        self.assertEqual(state["selected"], self.tabs[0]["id"])
        terminal = Request(url + "/api/input",
                           data=json.dumps({"tab": self.tabs[0]["id"], "key": "Escape",
                                            'pane': self.tabs[0]['panes'][0]['id'],
                                            'identity': self.tabs[0]['panes'][0]['identity']}).encode(),
                           headers={"Content-Type": "application/json"})
        with urlopen(terminal) as response:
            self.assertEqual(json.load(response)["selected"], self.tabs[0]["id"])
        self.assertEqual(len(self.payloads), 2)
        request.add_header("Origin", "https://unrelated.example")
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(len(self.payloads), 2)


class ActivityStateTests(unittest.TestCase):
    def test_activity_state_uses_desk_status_semantics(self):
        self.assertEqual(activity_state("working", "", "codex"), "working")
        self.assertEqual(activity_state("done", "", "codex"), "ready")
        self.assertEqual(activity_state("", "1", "codex"), "needs_you")
        self.assertEqual(activity_state("", "", "codex"), "idle")
        self.assertEqual(activity_state("", "", "python3"), "open")


class JevAdapterTests(unittest.TestCase):
    def test_boundary_cases_copy_exact_source_spans_without_cleanup(self):
        cases = json.loads(Path(__file__).with_name("boundary_cases.json").read_text())
        for case in cases:
            if "message" not in case:
                continue
            with self.subTest(request=case["request"]):
                request, message = case["request"], case["message"]
                start = request.index(message)
                payload = jev.question(request, [{"id": "@5"}], "@5")
                response = {"answers": {"message_start": {"choice": str(start)},
                                        "message_end": {"choice": str(start + len(message))}}}
                self.assertEqual(jev.message_text(request, payload, response), message)

    def test_api_request_contract(self):
        payload = jev.question("Go to Alpha", [{"id": "@0", "name": "Alpha", "index": 0}], "@0")
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "fake-test-only"}), \
                patch("jev.urlopen", return_value=io.BytesIO(json.dumps(answer("no_action")).encode())) as send:
            self.assertEqual(jev.evaluate(payload)["model"], "test-stub-not-jev")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-test-only")
        self.assertEqual(json.loads(request.data), payload)
        self.assertNotIn("fake-test-only", json.dumps(payload))
        self.assertEqual(set(payload["questions"]), {"action", "target", "pane:@0", "close_scope", "message_start", "message_end", "message_form", 'clarification_start', 'clarification_end'})

    def test_verbatim_span_preserves_multiline_and_punctuation(self):
        message = 'Reply with "Beta".\nDo not run $HOME `anything` — café.'
        request = 'Tell Luna: “' + message + '”'
        payload = jev.question(request, [{"id": "@5"}], "@5")
        response = {"answers": {"message_start": {"choice": "12"},
                                "message_end": {"choice": str(12 + len(message))}}}
        self.assertEqual(jev.message_text(request, payload, response), message)
        response["answers"]["message_end"]["choice"] = "0"
        with self.assertRaises(ValueError):
            jev.message_text(request, payload, response)


class ReplyTests(unittest.TestCase):
    def test_empty_history(self):
        self.assertEqual(latest_reply({"turns": []}), "No completed reply yet.")

    def test_only_latest_completed_final_answer(self):
        def turn(status, text, phase="final_answer"):
            return {"status": status, "items": [{"type": "agentMessage", "phase": phase, "text": text}]}
        history = {"turns": [turn("completed", "old"), turn("completed", "latest"),
                             turn("completed", "commentary", "commentary"),
                             turn("interrupted", "interrupted"), turn("failed", "failed"),
                             turn("inProgress", "partial")]}
        self.assertEqual(latest_reply(history), "latest")

    def test_missing_api_key_does_not_call_provider(self):
        with patch.dict("os.environ", {}, clear=True), patch("jev.urlopen") as send:
            with self.assertRaisesRegex(RuntimeError, "TYPESAFE_API_KEY"):
                jev.evaluate({})
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
