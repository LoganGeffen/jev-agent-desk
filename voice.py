import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, quote, urlsplit, parse_qs
from urllib.request import Request, urlopen

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect
from websockets.sync.server import serve


def load_voice_settings(path=None):
    names = ("TYPESAFE_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")
    if all(os.environ.get(name) for name in names):
        return
    path = path or os.environ.get("JEV_SETTINGS_FILE")
    if not path:
        return
    path = Path(path).expanduser()
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        key = key.strip()
        if not separator or key not in names or os.environ.get(key):
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise RuntimeError(f"Invalid {key} in voice settings") from None
        if len(parts) != 1:
            raise RuntimeError(f"Invalid {key} in voice settings")
        os.environ[key] = parts[0]


class Voice:
    def __init__(self, log):
        self.log = Path(log)
        self.lock = threading.Lock()
        self.url = None

    def record(self, kind, **fields):
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        with self.lock, self.log.open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    def config(self):
        return {"configured": bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("ELEVENLABS_VOICE_ID")),
                "websocket_url": self.url, "stt_model": "scribe_v2_realtime", "tts_model": "eleven_flash_v2_5"}

    def speak(self, text, profile='current'):
        if not self.config()["configured"]:
            raise RuntimeError("ElevenLabs key and voice ID are not configured")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError("Speech requires between 1 and 20000 characters")
        models = {'current': 'eleven_flash_v2_5', 'alternate': 'eleven_multilingual_v2'}
        if profile not in models:
            raise ValueError('Unknown voice rendering')
        model = models[profile]
        started = time.monotonic()
        voice_id = quote(os.environ["ELEVENLABS_VOICE_ID"], safe="")
        request = Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": model}).encode(),
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                audio = response.read()
        except HTTPError as error:
            self.record("tts_error", http_status=error.code)
            raise RuntimeError(f"ElevenLabs speech HTTP {error.code}") from None
        self.record("tts_generated", text=text, model=model, bytes=len(audio),
                    elapsed_ms=round((time.monotonic() - started) * 1000))
        return audio

    def speech_relay(self, browser, profile):
        models = {'current': 'eleven_flash_v2_5', 'alternate': 'eleven_multilingual_v2'}
        if profile not in models or not self.config()['configured']:
            browser.send(json.dumps({'error': 'Speech is unavailable'}))
            return
        params = urlencode({'model_id': models[profile], 'output_format': 'pcm_24000', 'auto_mode': 'true'})
        url = 'wss://api.elevenlabs.io/v1/text-to-speech/' + quote(os.environ['ELEVENLABS_VOICE_ID'], safe='') + '/stream-input?' + params
        started = time.monotonic()
        first_audio = None
        try:
            with connect(url, additional_headers={'xi-api-key': os.environ['ELEVENLABS_API_KEY']},
                         open_timeout=10, close_timeout=1) as provider:
                provider.send(json.dumps({'text': ' '}))

                def upload():
                    size = 0
                    try:
                        for raw in browser:
                            text = json.loads(raw)['text']
                            if not isinstance(text, str):
                                raise ValueError('Invalid speech text')
                            size += len(text)
                            if size > 24000:
                                raise ValueError('Speech text is too long')
                            provider.send(json.dumps({'text': text, **({'flush': True} if text else {})}))
                            if not text:
                                return
                        provider.close()
                    except (KeyError, ValueError, TypeError, ConnectionClosed):
                        provider.close()

                worker = threading.Thread(target=upload, daemon=True)
                worker.start()
                try:
                    for raw in provider:
                        data = json.loads(raw)
                        if data.get('audio'):
                            if first_audio is None:
                                first_audio = round((time.monotonic() - started) * 1000)
                            browser.send(json.dumps({'audio': data['audio']}))
                        if data.get('isFinal'):
                            browser.send(json.dumps({'isFinal': True}))
                            break
                        if data.get('error'):
                            raise RuntimeError('Speech provider rejected the stream')
                finally:
                    browser.close()
                    worker.join(timeout=2)
        except (OSError, TimeoutError, ConnectionClosed, InvalidStatus, RuntimeError):
            try:
                browser.send(json.dumps({'error': 'Speech stream failed'}))
            except ConnectionClosed:
                pass
        finally:
            self.record('tts_stream', first_audio_ms=first_audio,
                        elapsed_ms=round((time.monotonic() - started) * 1000))

    def relay(self, browser):
        profile = parse_qs(urlsplit(browser.request.path).query).get('tts')
        if profile:
            return self.speech_relay(browser, profile[0])
        if not self.config()["configured"]:
            browser.send(json.dumps({"message_type": "error", "error": "ElevenLabs is not configured"}))
            return
        params = urlencode({"model_id": "scribe_v2_realtime", "audio_format": "pcm_16000",
                            "commit_strategy": "vad", "vad_silence_threshold_secs": 1.2,
                            "language_code": "en"})
        started = time.monotonic()
        audio_chunks = 0
        audio_bytes = 0
        transcripts = 0
        try:
            with connect("wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + params,
                         additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                         open_timeout=10, close_timeout=2) as provider:
                def upload():
                    nonlocal audio_chunks, audio_bytes
                    try:
                        for raw in browser:
                            body = json.loads(raw)
                            if body.get("message_type") != "input_audio_chunk" or not isinstance(body.get("audio_base_64"), str):
                                raise ValueError("Invalid audio chunk")
                            chunk_bytes = len(base64.b64decode(body["audio_base_64"], validate=True))
                            provider.send(json.dumps({"message_type": "input_audio_chunk",
                                                      "audio_base_64": body["audio_base_64"], "sample_rate": 16000}))
                            audio_chunks += 1
                            audio_bytes += chunk_bytes
                            if audio_chunks == 1:
                                self.record('stt_audio_started', bytes=chunk_bytes, sample_rate=16000)
                    except (ValueError, TypeError, AttributeError, ConnectionClosed):
                        pass
                    finally:
                        provider.close()

                worker = threading.Thread(target=upload, daemon=True)
                worker.start()
                try:
                    for raw in provider:
                        body = json.loads(raw)
                        kind = body.get("message_type")
                        if kind == "session_started":
                            self.record("stt_connected", model="scribe_v2_realtime")
                            browser.send(json.dumps({"message_type": kind}))
                        elif kind in ("partial_transcript", "committed_transcript"):
                            transcripts += 1
                            self.record(kind, text=body["text"])
                            browser.send(json.dumps({"message_type": kind, "text": body["text"]}))
                        elif "error" in body or kind.endswith("error"):
                            self.record("stt_error", category=kind)
                            browser.send(json.dumps({"message_type": "error", "error": f"Speech recognition: {kind}"}))
                            break
                finally:
                    browser.close()
                    worker.join(timeout=3)
        except InvalidStatus as error:
            code = error.response.status_code
            self.record("stt_error", http_status=code)
            browser.send(json.dumps({"message_type": "error", "error": f"ElevenLabs recognition HTTP {code}"}))
        except ConnectionClosed:
            pass
        except (OSError, TimeoutError):
            self.record("stt_error", category="connection_failed")
            browser.send(json.dumps({"message_type": "error", "error": "Speech provider connection failed"}))
        finally:
            self.record("stt_disconnected", elapsed_ms=round((time.monotonic() - started) * 1000),
                        audio_chunks=audio_chunks, audio_bytes=audio_bytes, transcripts=transcripts)

    def start(self, origin, port=0, public_origin=None):
        server = serve(self.relay, "127.0.0.1", port, origins=[origin, *([public_origin] if public_origin else [])], max_size=65536, close_timeout=2)
        self.url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        if public_origin:
            self.url = public_origin.replace("https://", "wss://", 1) + "/voice"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
