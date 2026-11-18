"""Transactional records and audit events. No model/network calls."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def uid():
    return uuid.uuid4().hex


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def get(self, kind, key, default=None):
        row = self.conn.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind, value, key=None):
        key = str(key if key is not None else value["id"])
        self.conn.execute("INSERT INTO records(kind,id,data) VALUES(?,?,?) "
                          "ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                          (kind, key, json.dumps(value)))
        return value

    def all(self, kind):
        return [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,))]

    def event(self, action, **data):
        self.conn.execute("INSERT INTO events(time,action,data) VALUES(?,?,?)", (utc(), action, json.dumps(data)))


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.local = threading.local()
        with self.tx() as tx:
            tx.conn.executescript("CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,data TEXT NOT NULL,PRIMARY KEY(kind,id));"
                                  "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,time TEXT,action TEXT,data TEXT);")

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute("BEGIN IMMEDIATE")
                tx = Tx(conn)
                self.local.tx = tx
                yield tx
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                self.local.tx = None
                conn.close()

    def get(self, kind, key, default=None):
        if getattr(self.local, "tx", None):
            return self.local.tx.get(kind, key, default)
        with self.tx() as tx:
            return tx.get(kind, key, default)

    def all(self, kind):
        if getattr(self.local, "tx", None):
            return self.local.tx.all(kind)
        with self.tx() as tx:
            return tx.all(kind)

    def enqueue(self, project, thread, reason, *, key=None, priority=80, kind="supervisor", payload=None):
        key = key or uid()
        with self.tx() as tx:
            if tx.get("requests", key):
                return
            tx.put("requests", {"id": key, "project": project, "thread": thread, "reason": reason,
                               "priority": priority, "kind": kind, "payload": payload or {},
                               "status": "queued", "created": time.time()})

    def outbox(self, channel, thread, text, *, key=None, urgent=False, blocks=None):
        key = key or uid()
        with self.tx() as tx:
            if not tx.get("outbox", key):
                tx.put("outbox", {"id": key, "channel": channel, "thread": thread, "text": text,
                                  "urgent": urgent, "blocks": blocks, "status": "queued", "created": time.time()})
