from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading


class Diagnostics:
    def __init__(self, path, max_bytes=4 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.secret_values = tuple(value for key, value in os.environ.items() if len(value) >= 8 and
            any(part in key.upper() for part in ('API_KEY', 'TOKEN', 'PASSWORD', 'SECRET')))

    def record(self, stage, **fields):
        def scrub(value):
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items() if isinstance(key, str) and key.lower().replace('-', '_') not in
                        {'authorization', 'headers', 'api_key', 'xi_api_key', 'typesafe_api_key', 'elevenlabs_api_key',
                         'access_token', 'refresh_token', 'token', 'password', 'secret', 'raw_audio', 'audio_base_64'}}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, str):
                for secret in self.secret_values:
                    value = value.replace(secret, '[credential redacted]')
                return re.sub(r'(?i)\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{16,}', '[credential redacted]', value)
            return value
        try:
            event = {'timestamp': datetime.now(timezone.utc).isoformat(), 'stage': stage, **scrub(fields)}
            data = (json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode()
            event_limit = min(128000, self.max_bytes)
            if len(data) > event_limit:
                correlation = {key: event[key] for key in ('capture_id', 'client_id', 'request_id', 'turn_id',
                    'transcript_id', 'transcript_revision', 'playback_id', 'job_id', 'session_id', 'generation', 'socket_epoch') if key in event}
                event = {'timestamp': event['timestamp'], 'stage': stage, **correlation, 'truncated': True,
                         'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                         'preview': data[:event_limit // 4].decode('utf-8', errors='replace')}
                data = (json.dumps(event, ensure_ascii=False) + '\n').encode()
            with self.lock:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                    self.path.replace(self.path.with_suffix(self.path.suffix + '.1'))
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, 'ab') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
            return True
        except Exception:
            return False
