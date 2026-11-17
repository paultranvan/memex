"""Hermes sessions: SQLite database ~/.hermes/state.db, opened read-only.

The cursor is the id of the last message read. Mostly useful on the server,
to capture the conversations held over messaging.
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from ..segment import Chunk
from ..util import parse_ts
from . import Adapter, SessionRef, clean_human, is_pipeline, mark_final


class HermesAdapter(Adapter):
    name = "hermes"

    def __init__(self, db_path: Path, exclude_sources: list[str]):
        self.db = Path(db_path)
        self.exclude = {s.lower() for s in exclude_sources}

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()  # `with conn` alone would only end the transaction

    def discover(self) -> Iterator[SessionRef]:
        if not self.db.is_file():
            return
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT s.id, s.source, COALESCE(s.last_activity_at, s.ended_at, s.started_at) AS seen,"
                    " (SELECT COALESCE(MAX(m.id), 0) FROM messages m WHERE m.session_id = s.id) AS last_id"
                    " FROM sessions s"
                ).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            if (row["source"] or "").lower() in self.exclude:
                continue
            seen = parse_ts(row["seen"])
            # `size` carries the last message id: it grows as the session moves on.
            yield SessionRef(self.name, str(row["id"]), self.db, seen.timestamp() if seen else 0.0, int(row["last_id"] or 0))

    def read(self, ref: SessionRef, cursor: Any, meta: dict) -> Chunk:
        last = int(cursor or 0)
        meta = dict(meta or {})
        events: list[tuple] = []
        skip = meta.get("skip", "")
        try:
            with self._connect() as conn:
                session = conn.execute(
                    "SELECT title, cwd, git_branch, source FROM sessions WHERE id = ?", (ref.session,)
                ).fetchone()
                rows = conn.execute(
                    "SELECT id, role, content, tool_calls, timestamp FROM messages"
                    " WHERE session_id = ? AND id > ? ORDER BY id",
                    (ref.session, last),
                ).fetchall()
        except sqlite3.Error as exc:
            return Chunk(cursor=last, meta=meta, skip=f"Hermes database unreadable: {exc}")
        if session:
            if session["title"]:
                meta["title"] = session["title"]
            if session["cwd"]:
                meta.setdefault("cwd", session["cwd"])
            if session["git_branch"]:
                meta["branch"] = session["git_branch"]
            meta["channel"] = session["source"] or ""
        for row in rows:
            last = max(last, int(row["id"]))
            stamp = parse_ts(row["timestamp"])
            content = row["content"] if isinstance(row["content"], str) else ""
            if row["role"] == "user":
                text = clean_human(content)
                if text:
                    if is_pipeline(text):
                        skip = meta["skip"] = "memex pipeline session"
                    events.append(("human", stamp, text))
            elif row["role"] == "assistant":
                if content.strip():
                    events.append(("assistant", stamp, content.strip()))
                if row["tool_calls"]:
                    events.append(("tool",))
            elif row["role"] == "tool":
                events.append(("tool",))
        return Chunk(messages=mark_final(events), meta=meta, cursor=last, skip=skip)
