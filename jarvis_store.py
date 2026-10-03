import json
from pathlib import Path
import sqlite3
import threading


COLLECTIONS = {'turns', 'requests', 'drafts', 'actions', 'jobs', 'playbacks', 'clarifications', 'evidence', 'notices', 'decisions', 'persistence_errors'}


class JarvisStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id))')
        self.connection.commit()
        self.cache = {}

    def load(self):
        with self.lock:
            rows = self.connection.execute('SELECT kind,id,value FROM records ORDER BY rowid').fetchall()
            self.cache = {(kind, identifier): value for kind, identifier, value in rows}
        if not rows:
            return None
        snapshot = {kind: {} for kind in COLLECTIONS}
        for kind, identifier, value in rows:
            if kind == 'meta':
                snapshot[identifier] = json.loads(value)
            else:
                snapshot[kind][identifier] = json.loads(value)
        return snapshot

    def checkpoint(self, snapshot):
        current = {}
        for kind, value in snapshot.items():
            if kind in COLLECTIONS:
                current.update({(kind, identifier): json.dumps(record, ensure_ascii=False, allow_nan=False)
                                for identifier, record in value.items()})
            else:
                current[('meta', kind)] = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self.lock:
            changed = [(kind, identifier, value) for (kind, identifier), value in current.items()
                       if self.cache.get((kind, identifier)) != value]
            removed = [key for key in self.cache if key not in current]
            try:
                with self.connection:
                    self.connection.executemany('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value', changed)
                    self.connection.executemany('DELETE FROM records WHERE kind=? AND id=?', removed)
            except sqlite3.Error as error:
                raise RuntimeError('Jarvis checkpoint failed: ' + str(error)) from error
            self.cache = current

    def close(self):
        with self.lock:
            self.connection.close()
