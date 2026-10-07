import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import requests
from pywebpush import webpush


DEVICE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
DEFAULT_SUBJECT = "mailto:jev-agent-desk@example.invalid"


class PushError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class NoRedirectSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_b64url(value, label, length):
    if not isinstance(value, str) or not value or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise PushError(f"Invalid push {label} key")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise PushError(f"Invalid push {label} key") from error
    if len(decoded) != length:
        raise PushError(f"Invalid push {label} key")
    return value.rstrip("=")


def validate_device_id(value):
    if not isinstance(value, str) or not DEVICE_ID.fullmatch(value):
        raise PushError("Invalid notification device ID")
    return value


def validate_endpoint(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise PushError("Invalid push endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise PushError("Invalid push endpoint") from error
    host = parsed.hostname
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment
            or port not in (None, 443) or host.endswith(".") or not parsed.path.startswith("/")):
        raise PushError("Invalid push endpoint")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PushError("Push endpoints cannot be IP addresses")
    host = host.lower()
    if host not in PUSH_HOSTS:
        raise PushError("Unsupported push service")
    return value


def validate_subscription(value):
    if not isinstance(value, dict) or set(value) != {"endpoint", "keys"}:
        raise PushError("Invalid push subscription")
    keys = value["keys"]
    if not isinstance(keys, dict) or set(keys) != {"auth", "p256dh"}:
        raise PushError("Invalid push subscription keys")
    endpoint = validate_endpoint(value["endpoint"])
    auth = _decode_b64url(keys["auth"], "auth", 16)
    p256dh = _decode_b64url(keys["p256dh"], "p256dh", 65)
    if base64.urlsafe_b64decode(p256dh + "=" * (-len(p256dh) % 4))[0] != 4:
        raise PushError("Invalid push p256dh key")
    return {"endpoint": endpoint, "keys": {"auth": auth, "p256dh": p256dh}}


def validate_target(value):
    if not isinstance(value, dict) or set(value) != {"tab", "pane", "identity"}:
        raise PushError("Invalid notification conversation")
    if any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 512
           for key in ("tab", "pane", "identity")):
        raise PushError("Invalid notification conversation")
    return {key: value[key] for key in ("tab", "pane", "identity")}


def conversation_key(value):
    if not isinstance(value, dict):
        raise PushError("Invalid notification conversation")
    stable = {"provider": value.get("provider"), "thread_id": value.get("thread_id")}
    if not stable["provider"] and "identity" in value:
        target = validate_target(value)
        try:
            _, stable["provider"], stable["thread_id"] = target["identity"].rsplit(":", 2)
        except ValueError as error:
            raise PushError("Invalid notification conversation") from error
    if (stable["provider"] not in ("claude", "codex") or not isinstance(stable["thread_id"], str)
            or not stable["thread_id"] or len(stable["thread_id"]) > 512):
        raise PushError("Invalid notification conversation")
    return stable


def conversation_id(value):
    stable = conversation_key(value)
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def vapid_subject(origin):
    if not origin:
        return DEFAULT_SUBJECT
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return DEFAULT_SUBJECT
    if (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
            and (port is None or 0 < port < 65536)):
        return origin.rstrip("/")
    return DEFAULT_SUBJECT


def _load_or_create_key(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        descriptor, temporary = tempfile.mkstemp(prefix=".vapid-", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return key
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise PushError("The VAPID private key is not a private regular file", 500)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            key = serialization.load_pem_private_key(stream.read(), password=None)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise PushError("The VAPID private key is invalid", 500)
    return key


def _default_sender(subscription, payload, private_key, subject):
    session = NoRedirectSession()
    try:
        webpush(subscription, payload, vapid_private_key=str(private_key),
                vapid_claims={"sub": subject}, ttl=86400, timeout=10,
                requests_session=session)
    finally:
        session.close()


class PushNotifications:
    def __init__(self, run_dir, public_origin=None, sender=None, test_delay=2):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "push.sqlite3"
        self.private_key_path = self.run_dir / "vapid-private.pem"
        self.private_key = _load_or_create_key(self.private_key_path)
        public = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = _b64url(public)
        self.subject = vapid_subject(public_origin)
        self.sender = sender or _default_sender
        self.test_delay = test_delay
        self._initialize()

    def configure_origin(self, public_origin):
        self.subject = vapid_subject(public_origin)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    device_id TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mutes (
                    device_id TEXT NOT NULL REFERENCES subscriptions(device_id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    PRIMARY KEY (device_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    conversation_id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    target TEXT,
                    turn_id TEXT
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT NOT NULL REFERENCES events(event_id),
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempted_at REAL,
                    detail TEXT,
                    opened_at REAL,
                    open_status TEXT,
                    PRIMARY KEY (event_id, device_id)
                );
            """)
            connection.execute("UPDATE deliveries SET status='uncertain', detail='process restarted during push delivery' WHERE status IN ('pending', 'sending')")

    def state(self, device_id, agent=None):
        device_id = validate_device_id(device_id)
        identifier = conversation_id(agent) if agent is not None else None
        with self._connect() as connection:
            enabled = connection.execute("SELECT 1 FROM subscriptions WHERE device_id=?", (device_id,)).fetchone() is not None
            muted = False
            if enabled and identifier:
                muted = connection.execute(
                    "SELECT 1 FROM mutes WHERE device_id=? AND conversation_id=?",
                    (device_id, identifier)).fetchone() is not None
        return {"public_key": self.public_key, "enabled": enabled, "muted": muted}

    def subscribe(self, device_id, subscription):
        device_id = validate_device_id(device_id)
        subscription = validate_subscription(subscription)
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM subscriptions WHERE endpoint=? AND device_id<>?",
                               (subscription["endpoint"], device_id))
            connection.execute("""
                INSERT INTO subscriptions(device_id, endpoint, p256dh, auth, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint,
                    p256dh=excluded.p256dh, auth=excluded.auth, updated_at=excluded.updated_at
            """, (device_id, subscription["endpoint"], subscription["keys"]["p256dh"],
                  subscription["keys"]["auth"], now, now))
        return {"enabled": True}

    def unsubscribe(self, device_id):
        device_id = validate_device_id(device_id)
        with self._connect() as connection:
            connection.execute("DELETE FROM subscriptions WHERE device_id=?", (device_id,))
        return {"enabled": False}

    def mute(self, device_id, agent, muted):
        device_id = validate_device_id(device_id)
        stable = conversation_key(agent)
        identifier = conversation_id(stable)
        if type(muted) is not bool:
            raise PushError("muted must be a boolean")
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM subscriptions WHERE device_id=?", (device_id,)).fetchone() is None:
                raise PushError("Notifications are not enabled on this device", 409)
            if muted:
                connection.execute("INSERT OR REPLACE INTO mutes(device_id, conversation_id, target) VALUES (?, ?, ?)",
                                   (device_id, identifier, json.dumps(stable, sort_keys=True)))
            else:
                connection.execute("DELETE FROM mutes WHERE device_id=? AND conversation_id=?",
                                   (device_id, identifier))
        return {"muted": muted}

    @staticmethod
    def _agent_target(agent):
        return validate_target({"tab": agent["window_id"], "pane": agent["pane_id"],
                                "identity": f"{agent['process']}:{agent.get('provider', 'codex')}:{agent['thread_id']}"})

    def observe(self, agent, turn_id, notify=True):
        target = self._agent_target(agent)
        identifier = conversation_id(agent)
        if isinstance(turn_id, (list, tuple)) and turn_id:
            turn_id = turn_id[0]
        if turn_id is not None and not isinstance(turn_id, str):
            raise PushError("Invalid notification turn")
        turn_id = turn_id or ""
        now = time.time()
        event_id = None
        pending = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            baseline = connection.execute("SELECT turn_id FROM baselines WHERE conversation_id=?", (identifier,)).fetchone()
            if baseline is None:
                connection.execute("INSERT INTO baselines(conversation_id, target, turn_id, observed_at) VALUES (?, ?, ?, ?)",
                                   (identifier, json.dumps(target, sort_keys=True), turn_id, now))
                return None
            if not turn_id or baseline["turn_id"] == turn_id:
                return None
            connection.execute("UPDATE baselines SET turn_id=?, target=?, observed_at=? WHERE conversation_id=?",
                               (turn_id, json.dumps(target, sort_keys=True), now, identifier))
            if not notify:
                return None
            event_id = "reply:" + hashlib.sha256(f"{identifier}\0{turn_id}".encode()).hexdigest()
            label = re.sub(r"[\x00-\x1f\x7f]", " ", str(agent.get("label") or "Agent")).strip()[:80] or "Agent"
            created = connection.execute("""
                INSERT OR IGNORE INTO events(event_id, kind, observed_at, title, body, target, turn_id)
                VALUES (?, 'reply', ?, ?, 'Agent replied', ?, ?)
            """, (event_id, now, label, json.dumps(target, sort_keys=True), turn_id)).rowcount
            if created:
                devices = connection.execute("""
                    SELECT device_id, EXISTS (
                        SELECT 1 FROM mutes WHERE mutes.device_id=subscriptions.device_id
                        AND mutes.conversation_id=?
                    ) AS muted FROM subscriptions
                """, (identifier,)).fetchall()
                for device in devices:
                    connection.execute("INSERT INTO deliveries(event_id, device_id, status) VALUES (?, ?, ?)",
                                       (event_id, device["device_id"], "muted" if device["muted"] else "pending"))
                pending = any(not device["muted"] for device in devices)
        if pending:
            self._spawn(lambda: self._deliver(event_id))
        return event_id

    def test(self, device_id):
        device_id = validate_device_id(device_id)
        event_id = "test:" + uuid.uuid4().hex
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM subscriptions WHERE device_id=?", (device_id,)).fetchone() is None:
                raise PushError("Notifications are not enabled on this device", 409)
            connection.execute("INSERT INTO events(event_id, kind, observed_at, title, body) VALUES (?, 'test', ?, 'Jev Agent Desk', 'Test notification')",
                               (event_id, time.time()))
            connection.execute("INSERT INTO deliveries(event_id, device_id, status) VALUES (?, ?, 'pending')",
                               (event_id, device_id))
        timer = threading.Timer(self.test_delay, self._safe_dispatch,
                                args=(lambda: self._deliver(event_id),))
        timer.daemon = True
        timer.start()
        return {"queued": True, "delay_seconds": self.test_delay}

    def open(self, device_id, event_id, target, status, validate_live):
        device_id = validate_device_id(device_id)
        if not isinstance(event_id, str) or not event_id.startswith("reply:") or len(event_id) != 70:
            raise PushError("Invalid notification event")
        target = validate_target(target)
        if status not in ("opened", "stale"):
            raise PushError("Invalid notification open status")
        with self._connect() as connection:
            event = connection.execute("SELECT target FROM events WHERE event_id=? AND kind='reply'", (event_id,)).fetchone()
            delivery = connection.execute("SELECT status FROM deliveries WHERE event_id=? AND device_id=?",
                                          (event_id, device_id)).fetchone()
            if (not event or not delivery or delivery["status"] not in ("sending", "sent")
                    or json.loads(event["target"]) != target):
                raise PushError("That notification is unavailable or changed", 409)
        if status == "opened":
            validate_live(target["tab"], target["pane"], target["identity"])
        with self._connect() as connection:
            connection.execute("UPDATE deliveries SET opened_at=?, open_status=? WHERE event_id=? AND device_id=?",
                               (time.time(), status, event_id, device_id))
        return {"status": status}

    def _spawn(self, target):
        threading.Thread(target=self._safe_dispatch, args=(target,), daemon=True,
                         name="web-push-delivery").start()

    @staticmethod
    def _safe_dispatch(target):
        try:
            target()
        except Exception:
            pass

    def _payload(self, event):
        data = {"title": event["title"], "body": event["body"], "event_id": event["event_id"]}
        if event["target"]:
            target = json.loads(event["target"])
            query = urlencode({"notification": event["event_id"], **target})
            data.update(target=target, url="/?" + query)
        else:
            data["url"] = "/"
        payload = json.dumps(data, separators=(",", ":"))
        if len(payload.encode()) > 3072:
            raise PushError("Notification payload is too large", 500)
        return payload

    def _deliver(self, event_id):
        with self._connect() as connection:
            event = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not event:
            return
        payload = self._payload(event)
        while True:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                delivery = connection.execute("""
                    SELECT deliveries.device_id, subscriptions.endpoint, subscriptions.p256dh, subscriptions.auth
                    FROM deliveries JOIN subscriptions USING(device_id)
                    WHERE deliveries.event_id=? AND deliveries.status='pending' LIMIT 1
                """, (event_id,)).fetchone()
                if delivery is None:
                    return
                claimed = connection.execute("""
                    UPDATE deliveries SET status='sending', attempted_at=?
                    WHERE event_id=? AND device_id=? AND status='pending'
                """, (time.time(), event_id, delivery["device_id"])).rowcount
            if not claimed:
                continue
            status, detail = "sent", None
            try:
                subscription = validate_subscription({"endpoint": delivery["endpoint"], "keys": {
                    "p256dh": delivery["p256dh"], "auth": delivery["auth"]}})
                self.sender(subscription, payload, self.private_key_path, self.subject)
            except Exception as error:
                response = getattr(error, "response", None)
                code = getattr(response, "status_code", None)
                status = "revoked" if code in (404, 410) else "failed"
                detail = f"HTTP {code}" if code else type(error).__name__
            with self._connect() as connection:
                connection.execute("UPDATE deliveries SET status=?, detail=? WHERE event_id=? AND device_id=? AND status='sending'",
                                   (status, detail, event_id, delivery["device_id"]))
                if status == "revoked":
                    connection.execute("DELETE FROM subscriptions WHERE device_id=?", (delivery["device_id"],))
