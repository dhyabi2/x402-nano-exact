"""Durable, single-writer consumption store backed by SQLite.

A confirmed send hash may be redeemed by exactly one protected request. The
store makes that decision atomic with a ``UNIQUE(send_hash)`` constraint: the
first ``INSERT`` of a hash wins; a concurrent ``INSERT`` of the same hash for a
different request is rejected by the uniqueness violation and surfaced as
``ConcurrentBind``.

SQLite is deliberately single-writer-focused: a small stateless resource
server runs one consumer at a time, and an exclusive write transaction makes
the compare-and-set simple and local. A store that must be read by many
processes can implement ``ConsumptionStore`` over a shared DB.
"""

from __future__ import annotations

import sqlite3

from .confirmation import require_hex_64
from .consumption import (
    ConcurrentBind,
    ConsumptionRow,
    ConsumptionStore,
)


class SqliteConsumptionStore(ConsumptionStore):
    """SQLite-backed consumption store (durable across restarts)."""

    def __init__(self, path: str = ":memory:", *, timeout: float = 5.0) -> None:
        self._conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consumption (
                send_hash      TEXT PRIMARY KEY,
                request_key    TEXT NOT NULL,
                status         TEXT NOT NULL CHECK (status IN ('pending','completed','outcome_unknown')),
                response_handle TEXT,
                created_at     REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def bind(self, row: ConsumptionRow) -> ConsumptionRow:
        self._validate(row)
        try:
            self._conn.execute(
                "INSERT INTO consumption(send_hash, request_key, status, response_handle, created_at)"
                " VALUES (?,?,?,?,?)",
                (row.send_hash, row.request_key, row.status, row.response_handle, row.created_at),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as error:
            self._conn.rollback()
            existing = self.get(row.send_hash)
            if existing is None:  # pragma: no cover - unexpected
                raise ConcurrentBind(f"payment {row.send_hash} could not be bound") from error
            raise ConcurrentBind(
                f"payment {row.send_hash} already bound to request {existing.request_key}"
            ) from error
        return self.get(row.send_hash) or row

    def complete(self, send_hash: str, response_handle: str) -> ConsumptionRow:
        self._conn.execute(
            "UPDATE consumption SET status='completed', response_handle=? WHERE send_hash=?",
            (response_handle, send_hash),
        )
        self._conn.commit()
        return self.get(send_hash) or ConsumptionRow(
            send_hash=send_hash,
            request_key="",
            status="completed",
            response_handle=response_handle,
        )

    def resolve_unknown(self, send_hash: str) -> None:
        self._conn.execute(
            "UPDATE consumption SET status='outcome_unknown' WHERE send_hash=? AND status='pending'",
            (send_hash,),
        )
        self._conn.commit()

    def get(self, send_hash: str) -> ConsumptionRow | None:
        row = self._conn.execute(
            "SELECT send_hash, request_key, status, response_handle, created_at"
            " FROM consumption WHERE send_hash=?",
            (send_hash,),
        ).fetchone()
        if row is None:
            return None
        return ConsumptionRow(
            send_hash=row[0],
            request_key=row[1],
            status=row[2],
            response_handle=row[3],
            created_at=row[4],
        )

    @staticmethod
    def _validate(row: ConsumptionRow) -> None:
        require_hex_64(row.send_hash)
        if not row.request_key:
            raise ValueError("request_key is required")