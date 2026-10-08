import base64
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from push_notifications import (NoRedirectSession, PushError, PushNotifications,
                                validate_endpoint, validate_subscription, vapid_subject)
from reply_watch import ReadbackPreparation
from server import make_server
import test_playground
from test_readback_preparation import agent as watch_agent, turn


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/fixture"):
    return {"endpoint": endpoint, "keys": {
        "auth": encoded(b"a" * 16), "p256dh": encoded(b"\x04" + b"b" * 64)}}


def device(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def observed_agent():
    return {"window_id": "@1", "pane_id": "%2", "provider": "codex",
            "thread_id": "thread", "process": "100:200", "label": "Build"}


class PushStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jev-push-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sent = []
        self.push = PushNotifications(self.root, sender=lambda *args: self.sent.append(args), test_delay=0)
        self.push._spawn = lambda job: job()

    def test_primes_old_reply_then_sends_each_new_reply_once_across_restart(self):
        self.push.subscribe(device(1), subscription())
        agent = observed_agent()
        agent.update(label="Build, pane 2", tab_name="Build")
        self.assertIsNone(self.push.observe(agent, "old"))
        self.assertEqual(self.sent, [])
        event = self.push.observe(agent, "new")
        self.assertTrue(event.startswith("reply:"))
        self.assertEqual(len(self.sent), 1)
        payload = json.loads(self.sent[0][1])
        self.assertEqual(payload["title"], "Build")
        self.assertNotIn("body", payload)
        self.assertNotIn("Sensitive", self.sent[0][1])
        self.assertEqual(payload["target"], {"tab": "@1", "pane": "%2", "identity": "100:200:codex:thread"})
        self.push.observe(agent, "new")
        self.assertEqual(len(self.sent), 1)

        reopened = PushNotifications(self.root, sender=lambda *args: self.sent.append(args), test_delay=0)
        reopened._spawn = lambda job: job()
        reopened.observe(agent, "new")
        self.assertEqual(len(self.sent), 1)
        reopened.observe(agent, "newer")
        self.assertEqual(len(self.sent), 2)
        resumed = {**agent, "process": "101:201"}
        reopened.observe(resumed, "newer")
        self.assertEqual(len(self.sent), 2)
        reopened.observe(resumed, "after-resume")
        self.assertEqual(len(self.sent), 3)
        with closing(sqlite3.connect(self.root / "push.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events WHERE kind='reply'").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT status FROM deliveries ORDER BY rowid").fetchall(),
                             [("sent",), ("sent",), ("sent",)])

    def test_new_device_gets_no_backfill_and_per_device_mute_persists(self):
        agent = observed_agent()
        self.push.subscribe(device(1), subscription())
        self.push.observe(agent, None)
        self.push.observe(agent, "one")
        self.push.subscribe(device(2), subscription("https://updates.push.services.mozilla.com/wpush/v2/fixture"))
        self.push.observe(agent, "one")
        self.assertEqual(len(self.sent), 1)
        self.push.mute(device(1), agent, True)
        self.push.observe(agent, "two")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("mozilla", self.sent[-1][0]["endpoint"])
        reopened = PushNotifications(self.root, sender=lambda *args: self.sent.append(args), test_delay=0)
        resumed = {**agent, "process": "101:201"}
        self.assertTrue(reopened.state(device(1), resumed)["muted"])
        self.assertTrue(reopened.state(device(1), self.push._agent_target(resumed))["muted"])
        self.assertFalse(reopened.state(device(2), resumed)["muted"])
        reopened._spawn = lambda job: job()
        reopened.observe(resumed, "two")
        reopened.observe(resumed, "three")
        self.assertEqual(len(self.sent), 3)
        self.assertIn("mozilla", self.sent[-1][0]["endpoint"])
        with closing(sqlite3.connect(self.root / "push.sqlite3")) as connection:
            latest = connection.execute("SELECT event_id FROM events WHERE turn_id='two'").fetchone()[0]
            self.assertEqual(connection.execute("SELECT device_id, status FROM deliveries WHERE event_id=? ORDER BY device_id",
                                                (latest,)).fetchall(), [(device(1), "muted"), (device(2), "sent")])

    def test_revoked_subscription_is_removed_and_delivery_record_retained(self):
        class Gone(Exception):
            response = type("Response", (), {"status_code": 410})()
        self.push.sender = lambda *_: (_ for _ in ()).throw(Gone())
        self.push.subscribe(device(1), subscription())
        agent = observed_agent()
        self.push.observe(agent, None)
        self.push.observe(agent, "one")
        self.assertFalse(self.push.state(device(1))["enabled"])
        with closing(sqlite3.connect(self.root / "push.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT status, detail FROM deliveries").fetchone(), ("revoked", "HTTP 410"))

    def test_vapid_key_is_private_persistent_and_public_only_in_state(self):
        mode = os.stat(self.root / "vapid-private.pem").st_mode & 0o777
        self.assertEqual(mode, 0o600)
        public = self.push.public_key
        reopened = PushNotifications(self.root, sender=Mock())
        self.assertEqual(reopened.public_key, public)
        state = reopened.state(device(1))
        self.assertEqual(set(state), {"public_key", "enabled", "muted"})
        self.assertNotIn("private", json.dumps(state).lower())
        self.assertEqual(vapid_subject("https://desk.example.test"), "https://desk.example.test")
        self.assertTrue(vapid_subject("http://desk.example.test").startswith("mailto:"))
        reopened.configure_origin("https://desk.example.test:8443")
        self.assertEqual(reopened.subject, "https://desk.example.test:8443")

    def test_device_test_uses_the_persisted_delivery_transport(self):
        self.push.subscribe(device(1), subscription())
        target = self.push._agent_target(observed_agent())
        result = self.push.test(device(1), target, "Build")
        self.assertEqual(result, {"queued": True, "delay_seconds": 0})
        deadline = time.monotonic() + 2
        while not self.sent and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.sent), 1)
        payload = json.loads(self.sent[0][1])
        self.assertEqual(payload["title"], "Build")
        self.assertNotIn("body", payload)
        self.assertEqual(payload["target"], target)
        self.assertIn("tab=%401", payload["url"])
        self.push.open(device(1), payload["event_id"], target, "opened", lambda *_: None)
        status = None
        while status != "sent" and time.monotonic() < deadline:
            with closing(sqlite3.connect(self.root / "push.sqlite3")) as connection:
                self.assertEqual(connection.execute("SELECT kind FROM events").fetchone()[0], "test")
                status = connection.execute("SELECT status FROM deliveries").fetchone()[0]
            if status != "sent":
                time.sleep(.01)
        self.assertEqual(status, "sent")

    def test_open_requires_original_event_device_and_exact_target(self):
        self.push.subscribe(device(1), subscription())
        agent = observed_agent()
        target = {"tab": "@1", "pane": "%2", "identity": "100:200:codex:thread"}
        self.push.observe(agent, None)
        event = self.push.observe(agent, "one")
        validated = []
        result = self.push.open(device(1), event, target, "opened", lambda *args: validated.append(args))
        self.assertEqual(result, {"status": "opened"})
        self.assertEqual(validated, [("@1", "%2", "100:200:codex:thread")])
        with self.assertRaises(PushError):
            self.push.open(device(2), event, target, "opened", lambda *_: None)
        with closing(sqlite3.connect(self.root / "push.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT open_status FROM deliveries").fetchone()[0], "opened")

    def test_only_vetted_https_vendor_endpoints_and_valid_keys_are_accepted(self):
        for endpoint in ("http://fcm.googleapis.com/fcm/send/x", "https://127.0.0.1/x",
                         "https://evil.example/x", "https://fcm.googleapis.com.evil.example/x",
                         "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com:444/x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(PushError):
                validate_endpoint(endpoint)
        for endpoint in ("https://fcm.googleapis.com/fcm/send/x",
                         "https://updates.push.services.mozilla.com/wpush/v2/x",
                         "https://web.push.apple.com/x"):
            self.assertEqual(validate_endpoint(endpoint), endpoint)
        with self.assertRaises(PushError):
            validate_endpoint("https://wns2-bl2p.notify.windows.com/w/x")
        bad = subscription()
        bad["keys"]["auth"] = encoded(b"short")
        with self.assertRaises(PushError):
            validate_subscription(bad)

    def test_transport_forces_redirects_off(self):
        response = Mock()
        with patch.object(__import__("requests").Session, "request", return_value=response) as request:
            self.assertIs(NoRedirectSession().request("POST", "https://fcm.googleapis.com/x", allow_redirects=True), response)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])


class PushWatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jev-push-lifecycle-')
        self.addCleanup(temporary.cleanup)
        self.sent = []
        self.push = PushNotifications(Path(temporary.name), sender=lambda *args: self.sent.append(args))
        self.push._spawn = lambda job: job()
        self.push.subscribe(device(1), subscription())
        self.agent = {**watch_agent('thread'), **observed_agent()}
        self.activity = {'state': 'ready', 'turn_id': 'old'}
        self.thread = {'turns': [turn('old', 'Old reply')]}
        self.watcher = ReadbackPreparation(Mock(), on_reply=self.push.observe,
                                          activity=lambda *_: self.activity)

    def poll(self):
        with patch('reply_watch.history', side_effect=lambda _: self.thread), \
             patch('reply_watch.codex_actions.check_target'):
            self.watcher.poll([self.agent])

    def test_only_current_native_completed_reply_notifies(self):
        self.poll()
        self.assertEqual(self.sent, [])
        self.thread['turns'].append(turn('new', 'Still running', 'inProgress'))
        self.activity = {'state': 'working', 'turn_id': 'new'}
        self.poll()
        self.assertEqual(self.sent, [])
        self.thread['turns'][-1] = turn('new', 'New reply')
        self.poll()
        self.assertEqual(self.sent, [], 'A stored reply is not native turn completion')
        self.activity = {'state': 'ready', 'turn_id': 'new'}
        self.poll()
        self.poll()
        self.assertEqual(len(self.sent), 1)

    def test_old_completion_never_notifies_during_newer_turn_or_native_mismatch(self):
        self.poll()
        self.thread['turns'].extend([turn('middle', 'Missed reply'), turn('new', 'Active', 'inProgress')])
        self.activity = {'state': 'working', 'turn_id': 'new'}
        self.poll()
        self.activity = {'state': 'ready', 'turn_id': 'middle'}
        self.poll()
        self.assertEqual(self.sent, [])

    def test_interruption_failure_unknown_and_commentary_stay_silent(self):
        self.poll()
        for state, status, phase in [('stopped', 'interrupted', 'final_answer'),
                                     ('failed', 'failed', 'final_answer'),
                                     ('unknown', 'completed', 'final_answer'),
                                     ('ready', 'completed', 'commentary')]:
            self.thread = {'turns': [turn(state, 'Not a ready reply', status, phase)]}
            self.activity = {'state': state, 'turn_id': state}
            self.poll()
        self.assertEqual(self.sent, [])

    def test_restart_resume_and_unavailable_history_reprime_without_backfill(self):
        self.poll()
        self.thread = {'turns': [turn('offline', 'Reply during downtime')]}
        self.activity = {'state': 'ready', 'turn_id': 'offline'}
        self.watcher = ReadbackPreparation(Mock(), on_reply=self.push.observe,
                                          activity=lambda *_: self.activity)
        self.poll()
        self.agent = {**self.agent, 'process': '101:201'}
        self.thread = {'turns': [turn('resumed', 'Before resume')]}
        self.activity = {'state': 'ready', 'turn_id': 'resumed'}
        self.poll()
        with patch('reply_watch.history', side_effect=RuntimeError('unavailable')):
            self.watcher.poll([self.agent])
        self.thread = {'turns': [turn('recovered', 'Before recovery')]}
        self.activity = {'state': 'ready', 'turn_id': 'recovered'}
        self.poll()
        self.assertEqual(self.sent, [])
        self.thread = {'turns': [turn('fresh', 'Fresh reply')]}
        self.activity = {'state': 'ready', 'turn_id': 'fresh'}
        self.poll()
        self.assertEqual(len(self.sent), 1)

    def test_identity_change_after_native_read_suppresses_notification(self):
        self.poll()
        self.thread = {'turns': [turn('new', 'Reply')]}
        self.activity = {'state': 'ready', 'turn_id': 'new'}
        with patch('reply_watch.history', return_value=self.thread), \
             patch('reply_watch.codex_actions.check_target', side_effect=RuntimeError('changed')):
            self.watcher.poll([self.agent])
        self.assertEqual(self.sent, [])

    def test_replaced_lifecycle_file_reestablishes_silent_baseline(self):
        self.activity['generation'] = 'original'
        self.poll()
        self.thread = {'turns': [turn('rewritten', 'Historical after replacement')]}
        self.activity = {'state': 'ready', 'turn_id': 'rewritten', 'generation': 'replacement'}
        self.poll()
        self.assertEqual(self.sent, [])
        self.thread = {'turns': [turn('fresh', 'Current reply')]}
        self.activity = {'state': 'ready', 'turn_id': 'fresh', 'generation': 'replacement'}
        self.poll()
        self.assertEqual(len(self.sent), 1)

    def test_push_failure_cannot_stop_attention_or_readback_preparation(self):
        cache = Mock()
        callback = Mock(side_effect=RuntimeError("disk full"))
        watcher = ReadbackPreparation(cache, on_reply=callback)
        with patch("reply_watch.history", return_value={"turns": [turn("one", "Reply")]}):
            watcher.poll([watch_agent("one")])
        callback.assert_called_once()
        cache.prepare.assert_called_once_with("Reply")


class PushHttpTests(unittest.TestCase):
    def setUp(self):
        fixture = test_playground.PlaygroundTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.app = fixture.app
        self.app.push.test_delay = 0
        self.app.push.sender = Mock()
        self.server = make_server(self.app)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.session = self.app.jarvis.state.session_id

    def post(self, path, body, headers=None):
        request = Request(self.url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(request) as response:
            return response.status, json.load(response)

    def test_install_assets_root_query_and_device_lifecycle(self):
        for path, content_type in (("/?notification=reply%3Ax&tab=%401&pane=%252&identity=x", "text/html"),
                                   ("/manifest.json", "application/manifest+json"),
                                   ("/service-worker.js", "text/javascript"),
                                   ("/icon-192.png", "image/png"), ("/icon-512.png", "image/png")):
            with urlopen(self.url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers["Content-Type"])
                if path.startswith('/?'):
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertTrue(response.read())
        status, state = self.post("/api/push/state", {"session_id": self.session, "device_id": device(1)})
        self.assertEqual(status, 200)
        self.assertFalse(state["enabled"])
        self.assertEqual(set(state), {"public_key", "enabled", "muted"})
        self.post("/api/push/subscribe", {"session_id": self.session, "device_id": device(1),
                                          "subscription": subscription()})
        _, state = self.post("/api/push/state", {"session_id": self.session, "device_id": device(1)})
        self.assertTrue(state["enabled"])
        target = {"tab": "@1", "pane": "%2", "identity": "100:200:codex:thread"}
        self.app.validate_pane = Mock(return_value={**observed_agent(), "agent": True})
        self.post("/api/push/mute", {"session_id": self.session, "device_id": device(1),
                                     "target": target, "muted": True})
        _, state = self.post("/api/push/state", {"session_id": self.session,
                                                  "device_id": device(1), "target": target})
        self.assertTrue(state["muted"])
        _, queued = self.post("/api/push/test", {"session_id": self.session, "device_id": device(1),
                                                 "target": target})
        self.assertEqual(queued, {"queued": True, "delay_seconds": 0})
        self.post("/api/push/unsubscribe", {"session_id": self.session, "device_id": device(1)})
        _, state = self.post("/api/push/state", {"session_id": self.session, "device_id": device(1)})
        self.assertFalse(state["enabled"])

    def test_session_origin_unknown_fields_and_managed_browser_are_rejected(self):
        cases = [
            ({"session_id": "wrong", "device_id": device(1)}, {}, 409),
            ({"session_id": self.session, "device_id": device(1), "endpoint": "secret"}, {}, 400),
            ({"session_id": self.session, "device_id": device(1)}, {"Origin": "https://evil.example"}, 403),
            ({"session_id": self.session, "device_id": device(1)}, {"X-Jev-Managed-Browser": "1"}, 403),
        ]
        for body, headers, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(HTTPError) as error:
                self.post("/api/push/state", body, headers)
            self.assertEqual(error.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
