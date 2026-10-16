"""The project file: one SQLite database per project (``*.aptplan``).

Tables: meta, project (one JSON row per saved version, for undo), tickets, evidence, events.
Writes are atomic (SQLite transactions); the latest project row is the current state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import Evidence, Project, Ticket

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL,
                                    body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, ts REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, ts REAL NOT NULL,
                                     body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS evidence_key ON evidence(key);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
                                   body TEXT NOT NULL);
"""
MAX_VERSIONS = 200


class ProjectStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('format', 'aptplan/1')")

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------ project versions
    def save(self, project: Project, action: str) -> int:
        with self._lock:
            project.updated = time.time()
            cur = self.db.execute(
                "INSERT INTO project(ts, action, body) VALUES (?, ?, ?)",
                (project.updated, action, project.model_dump_json()),
            )
            seq = int(cur.lastrowid or 0)
            self.db.execute(
                "DELETE FROM project WHERE seq <= (SELECT MAX(seq) FROM project) - ?", (MAX_VERSIONS,)
            )
            return seq

    def load(self) -> Project | None:
        with self._lock:
            row = self.db.execute("SELECT body FROM project ORDER BY seq DESC LIMIT 1").fetchone()
            return Project.model_validate_json(row[0]) if row else None

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT seq, ts, action FROM project ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            return [{"seq": s, "ts": t, "action": a} for s, t, a in rows]

    def undo(self) -> Project | None:
        """Drop the latest version and return the one before it."""
        with self._lock:
            rows = self.db.execute("SELECT seq FROM project ORDER BY seq DESC LIMIT 2").fetchall()
            if len(rows) < 2:
                return None
            self.db.execute("DELETE FROM project WHERE seq = ?", (rows[0][0],))
            return self.load()

    # ------------------------------------------------------------------ tickets
    def put_ticket(self, t: Ticket) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO tickets(id, ts, body) VALUES (?, ?, ?)",
                            (t.id, t.created or time.time(), t.model_dump_json()))

    def tickets(self) -> list[Ticket]:
        with self._lock:
            rows = self.db.execute("SELECT body FROM tickets ORDER BY ts").fetchall()
            return [Ticket.model_validate_json(r[0]) for r in rows]

    def ticket(self, tid: str) -> Ticket | None:
        with self._lock:
            row = self.db.execute("SELECT body FROM tickets WHERE id = ?", (tid,)).fetchone()
            return Ticket.model_validate_json(row[0]) if row else None

    def delete_tickets(self, prefix: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM tickets WHERE id LIKE ?", (prefix + "%",))

    # ------------------------------------------------------------------ evidence
    def add_evidence(self, ev: Evidence) -> None:
        with self._lock:
            ev.ts = ev.ts or time.time()
            self.db.execute("INSERT INTO evidence(key, ts, body) VALUES (?, ?, ?)", (ev.key, ev.ts, ev.model_dump_json()))

    def evidence(self, key_prefix: str = "") -> list[Evidence]:
        with self._lock:
            rows = self.db.execute(
                "SELECT body FROM evidence WHERE key LIKE ? ORDER BY id", (key_prefix + "%",)
            ).fetchall()
            latest: dict[str, Evidence] = {}
            for (b,) in rows:
                e = Evidence.model_validate_json(b)
                latest[e.key] = e
            return list(latest.values())

    # ------------------------------------------------------------------ events (bridge handshake, logs)
    def event(self, kind: str, body: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute("INSERT INTO events(ts, kind, body) VALUES (?, ?, ?)", (time.time(), kind, json.dumps(body)))

    def events(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if kind:
                rows = self.db.execute("SELECT ts, kind, body FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?",
                                       (kind, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT ts, kind, body FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"ts": t, "kind": k, **json.loads(b)} for t, k, b in rows]
