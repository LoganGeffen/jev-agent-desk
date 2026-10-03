import json
from pathlib import Path
import subprocess
import threading
import unittest
from urllib.request import Request, urlopen

from server import make_server
from test_jarvis_tools import DisposableDesk, final

ROOT = Path(__file__).resolve().parent
PREFIX_ROUTE = "'heard_prefix'" in (ROOT / "server.py").read_text()


class PlaybackHTTPTests(DisposableDesk):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.session = self.app.jarvis.state.session_id

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def readback_turn(self, request_id):
        agent = self.app.jarvis_tools.agent(self.targets["Dog Data"])
        self.app.attention.observe(agent, self.history(agent["thread_id"]))
        self.provider = lambda context, specs, execute, **kwargs: execute("read_reply", {"name": "Dog Data"}) and final()
        body = {"session_id": self.session, "text": "Read Dog Data", "request_id": request_id, "source": "typed"}
        request = Request(self.url + "/api/jarvis/turn", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "Origin": self.url})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def drive(self, scenario, receipt):
        config = {"base": self.url, "session_id": self.session, "turn_id": receipt["turn_id"],
                  "original": receipt["readback"]["original"], "scenario": scenario}
        done = subprocess.run(["node", "playback_http_driver.mjs"], cwd=ROOT, input=json.dumps(config), text=True,
                              capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def queue(self):
        return self.app.attention.view(self.app.observe()["tabs"])

    def test_actual_client_and_server_partial_prefix_never_marks_the_reply_heard(self):
        receipt = self.readback_turn("partial")
        self.assertEqual(len(self.queue()), 1)
        outcome = self.drive("partial", receipt)
        self.assertGreaterEqual(len(outcome["phrases"]), 2)
        updates = [item for item in outcome["exchanges"] if item["body"]["op"] == "update"]
        self.assertEqual(updates[-1]["body"]["status"], "cancelled")
        self.assertEqual(updates[-1]["status"], 200)
        self.assertEqual(updates[-1]["body"]["completed_phrases"], [])
        self.assertEqual(len(self.queue()), 1)
        prefixed = [item for item in updates if "heard_prefix" in item["body"]]
        self.assertEqual(len(prefixed), 1)
        first = outcome["phrases"][0]
        self.assertEqual(prefixed[0]["body"]["heard_prefix"]["chars"], len(first))
        playback = self.app.jarvis.state.snapshot()["playbacks"][updates[-1]["body"]["playback_id"]]
        self.assertEqual(playback["status"], "cancelled")
        if PREFIX_ROUTE:
            self.assertEqual(prefixed[0]["status"], 200)
            self.assertEqual(playback["heard_prefix"]["chars"], len(first))
        else:
            self.assertEqual(prefixed[0]["status"], 400)
            self.assertNotIn("heard_prefix", playback)

    def test_actual_client_and_server_full_playback_acknowledges_the_exact_reply(self):
        receipt = self.readback_turn("full")
        self.assertEqual(len(self.queue()), 1)
        outcome = self.drive("full", receipt)
        updates = [item for item in outcome["exchanges"] if item["body"]["op"] == "update"]
        completed = updates[-1]
        self.assertEqual(completed["body"]["status"], "completed")
        self.assertEqual(completed["status"], 200)
        self.assertEqual(len(completed["body"]["completed_phrases"]), 1)
        self.assertNotIn("heard_prefix", completed["body"])
        self.assertTrue(all(item["status"] == 200 for item in updates if "heard_prefix" not in item["body"]))
        self.assertEqual(self.queue(), [])


if __name__ == "__main__":
    unittest.main()
