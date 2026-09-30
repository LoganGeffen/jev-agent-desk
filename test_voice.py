import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

import test_playground
from server import make_server
from voice import Voice, load_voice_settings


class VoiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="jev-voice-unit-")
        self.addCleanup(directory.cleanup)
        self.voice = Voice(Path(directory.name) / "voice.jsonl")

    def test_settings_load_only_approved_names_without_evaluating_shell(self):
        source = self.voice.log.with_name("settings")
        source.write_text("# test values only\nexport ELEVENLABS_API_KEY='literal-$(no-execution)'\n"
                          'ELEVENLABS_VOICE_ID="test-voice" # comment\nUNRELATED=ignored\n')
        with patch.dict(os.environ, {}, clear=True):
            load_voice_settings(source)
            self.assertEqual(dict(os.environ), {"ELEVENLABS_API_KEY": "literal-$(no-execution)",
                                               "ELEVENLABS_VOICE_ID": "test-voice"})

    def test_settings_preserve_environment_and_allow_missing_file(self):
        source = self.voice.log.with_name("settings")
        with patch.dict(os.environ, {}, clear=True):
            load_voice_settings(source)
            self.assertFalse(os.environ)
        source.write_text("TYPESAFE_API_KEY=file-typesafe\nELEVENLABS_API_KEY=file-key\nELEVENLABS_VOICE_ID=file-voice\n")
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "environment-key"}, clear=True):
            load_voice_settings(source)
            self.assertEqual(os.environ["ELEVENLABS_API_KEY"], "environment-key")
            self.assertEqual(os.environ["ELEVENLABS_VOICE_ID"], "file-voice")
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "file-typesafe")
            with patch.object(Path, "read_text") as read:
                load_voice_settings(source)
                read.assert_not_called()

    def test_typesafe_loads_on_cold_start_and_when_voice_is_already_configured(self):
        source = self.voice.log.with_name("settings")
        source.write_text("TYPESAFE_API_KEY=file-typesafe\n")
        for environment in ({}, {"ELEVENLABS_API_KEY": "voice-key", "ELEVENLABS_VOICE_ID": "voice-id"}):
            with self.subTest(environment_names=list(environment)), patch.dict(os.environ, environment, clear=True):
                load_voice_settings(source)
                self.assertEqual(os.environ["TYPESAFE_API_KEY"], "file-typesafe")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "explicit-key"}, clear=True):
            load_voice_settings(source)
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "explicit-key")

    def test_invalid_settings_error_does_not_expose_value(self):
        source = self.voice.log.with_name("settings")
        source.write_text("ELEVENLABS_API_KEY='fake-secret-without-closing-quote\n")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Invalid ELEVENLABS_API_KEY") as caught:
                load_voice_settings(source)
        self.assertNotIn("fake-secret", str(caught.exception))

    def test_speech_wire_contract_and_secret_exclusion(self):
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "fake-private-key", "ELEVENLABS_VOICE_ID": "test-voice"}), \
                patch("voice.urlopen", return_value=io.BytesIO(b"fake-audio")) as send:
            self.assertEqual(self.voice.speak("Hello Luna"), b"fake-audio")
            self.assertNotIn("fake-private-key", json.dumps(self.voice.config()))
        request = send.call_args.args[0]
        self.assertEqual(request.get_header("Xi-api-key"), "fake-private-key")
        self.assertEqual(json.loads(request.data), {"text": "Hello Luna", "model_id": "eleven_flash_v2_5"})
        self.assertIn("/text-to-speech/test-voice?", request.full_url)
        self.assertNotIn("fake-private-key", self.voice.log.read_text())

    def test_missing_key_and_invalid_text_do_not_call_provider(self):
        with patch.dict("os.environ", {}, clear=True), patch("voice.urlopen") as send:
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                self.voice.speak("Hello")
            send.assert_not_called()
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "fake", "ELEVENLABS_VOICE_ID": "voice"}):
            for text in [None, "", " " * 4, "x" * 20001]:
                with self.assertRaises(ValueError):
                    self.voice.speak(text)

    def test_provider_error_never_copies_raw_payload(self):
        error = HTTPError("url", 401, "private provider payload", {}, io.BytesIO(b"private provider payload"))
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "fake", "ELEVENLABS_VOICE_ID": "voice"}), \
                patch("voice.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
                self.voice.speak("Hello")
        self.assertNotIn("private", str(caught.exception) + self.voice.log.read_text())

    def test_websocket_rejects_other_origins_before_provider_connection(self):
        server = self.voice.start("http://127.0.0.1:12345")
        self.addCleanup(server.shutdown)
        with patch("voice.connect") as provider:
            with self.assertRaises(InvalidStatus) as caught:
                connect(self.voice.url, origin="https://unrelated.example")
            self.assertEqual(caught.exception.response.status_code, 403)
            provider.assert_not_called()


class VoiceHTTPTests(unittest.TestCase):
    def setUp(self):
        fixture = test_playground.PlaygroundTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.app = fixture.app

    def test_voice_endpoints(self):
        server = make_server(self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        for filename in ("voice.js", "microphone.js"):
            with urlopen(url + "/" + filename) as response:
                self.assertEqual(response.status, 200)
        request = Request(url + "/api/speech", data=b'{"text":"hello"}', headers={"Content-Type": "application/json"})
        with patch.object(self.app.voice, "speak", return_value=b"audio"):
            with urlopen(request) as response:
                self.assertEqual(response.read(), b"audio")
                self.assertEqual(response.headers.get_content_type(), "audio/mpeg")
            request.add_header("Origin", "https://unrelated.example")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
