import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen

from launch import attach_start, attach_status, attach_stop
from server import tmux


class AttachLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jev-attach-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.socket = str(self.root / "tmux.sock")
        self.run = self.root / "run"
        tmux(self.socket, "-f", "/dev/null", "new-session", "-d", "-s", "source",
             "-n", "One", "cat")
        self.first = tmux(self.socket, "display-message", "-p", "-t", "=source:",
                          "#{window_id}")
        self.second = tmux(self.socket, "new-window", "-d", "-P", "-F", "#{window_id}",
                           "-t", "source:", "-n", "Two", "cat")
        tmux(self.socket, "new-session", "-d", "-t", "source", "-s", "desktop")
        tmux(self.socket, "new-session", "-d", "-t", "source", "-s", "phone")
        self.addCleanup(tmux, self.socket, "kill-server")

    def post(self, url, path, body):
        request = Request(url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            return json.load(response)

    def test_attached_web_lifecycle_preserves_grouped_tmux_sessions(self):
        state = attach_start(self.socket, "source", "desktop", self.run,
                             reuse_private_auth=False)
        self.addCleanup(lambda: attach_stop(self.run) if (self.run / "owner.json").exists() else None)
        status = attach_status(self.run)
        self.assertEqual(status["socket"], self.socket)
        self.assertEqual(state["target"], {"source_session": "source", "view_session": "desktop"})

        selected = self.post(status["url"], "/api/select", {"tab": self.second})
        self.assertEqual(selected["selected"], self.second)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=desktop:",
                              "#{window_id}"), self.second)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=source:",
                              "#{window_id}"), self.first)
        self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", "=phone:",
                              "#{window_id}"), self.first)

        created = self.post(status["url"], "/api/tabs/create",
                            {"name": "Three", "group": "workspace"})["selected"]
        expected = set(tmux(self.socket, "list-windows", "-t", "=source",
                            "-F", "#{window_id}").splitlines())
        self.assertIn(created, expected)
        for session in ("desktop", "phone"):
            self.assertEqual(set(tmux(self.socket, "list-windows", "-t", f"={session}",
                                      "-F", "#{window_id}").splitlines()), expected)

        attach_stop(self.run)
        for session in ("source", "desktop", "phone"):
            self.assertEqual(tmux(self.socket, "display-message", "-p", "-t", f"={session}:",
                                  "#{session_name}"), session)
        self.assertFalse((self.run / "owner.json").exists())


if __name__ == "__main__":
    unittest.main()
