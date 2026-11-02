"""Offline tests for the pure consumption decision logic (single-use, reuse, crash)."""

from __future__ import annotations

import pytest

from x402_nano_exact.consumption import (
    ConcurrentBind,
    ConsumptionRow,
    ConsumptionStore,
    canonical_request_key,
    consume,
)

HASH = "B" * 64
REQ1 = canonical_request_key(method="POST", path="/api/paid", body_include=["prompt"], body={"prompt": "a"})
REQ2 = canonical_request_key(method="POST", path="/api/paid", body_include=["prompt"], body={"prompt": "b"})


class MemStore(ConsumptionStore):
    """In-memory store with the same atomic semantics as SQLite (single-writer)."""

    def __init__(self) -> None:
        self._rows: dict[str, ConsumptionRow] = {}

    def bind(self, row: ConsumptionRow) -> ConsumptionRow:
        if row.send_hash in self._rows:
            raise ConcurrentBind(f"already bound to {self._rows[row.send_hash].request_key}")
        self._rows[row.send_hash] = row
        return row

    def complete(self, send_hash: str, response_handle: str) -> ConsumptionRow:
        row = self._rows[send_hash]
        row.status = "completed"
        row.response_handle = response_handle
        return row

    def resolve_unknown(self, send_hash: str) -> None:
        row = self._rows[send_hash]
        if row.status == "pending":
            row.status = "outcome_unknown"

    def get(self, send_hash: str) -> ConsumptionRow | None:
        return self._rows.get(send_hash)


def test_first_bind_proceeds_and_records():
    store = MemStore()
    d = consume(store, send_hash=HASH, request_key=REQ1)
    assert d.kind == "proceed"
    assert store.get(HASH) is not None


def test_same_request_after_complete_replays_stored_outcome_not_rerun():
    store = MemStore()
    consume(store, send_hash=HASH, request_key=REQ1)
    store.complete(HASH, "200")
    d = consume(store, send_hash=HASH, request_key=REQ1)
    assert d.kind == "replay_completed"
    assert d.response_handle == "200"


def test_different_request_reusing_same_payment_is_rejected():
    store = MemStore()
    consume(store, send_hash=HASH, request_key=REQ1)
    store.complete(HASH, "200")
    d = consume(store, send_hash=HASH, request_key=REQ2)
    assert d.kind == "reuse_rejected"
    assert "consumed by another request" in d.reason


def test_inflight_same_request_is_unknown_not_rerun():
    # A concurrent/live pending row for the SAME request must not re-run.
    store = MemStore()
    consume(store, send_hash=HASH, request_key=REQ1)
    d = consume(store, send_hash=HASH, request_key=REQ1)
    assert d.kind == "outcome_unknown"


def test_inflight_different_request_is_rejected():
    store = MemStore()
    consume(store, send_hash=HASH, request_key=REQ1)
    d = consume(store, send_hash=HASH, request_key=REQ2)
    assert d.kind == "reuse_rejected"


def test_concurrent_bind_lost_by_different_request_is_rejected():
    # Simulate two requests seeing None then both binding; the loser must reject.
    store = MemStore()
    consume(store, send_hash=HASH, request_key=REQ1)  # wins
    # The loser of the race re-binds; store raises ConcurrentBind -> reuse_rejected
    d = consume(store, send_hash=HASH, request_key=REQ2)
    assert d.kind == "reuse_rejected"


def test_aged_out_pending_becomes_outcome_unknown():
    store = MemStore()
    # bind a row, then age it out
    consume(store, send_hash=HASH, request_key=REQ1)
    row = store.get(HASH)
    row.created_at = 1_000.0  # old timestamp
    store._rows[HASH] = row
    d = consume(store, send_hash=HASH, request_key=REQ1, age_out_seconds=30.0, now=2_000.0)
    assert d.kind == "outcome_unknown"
    assert store.get(HASH).status == "outcome_unknown"


def test_malformed_send_hash_raises():
    store = MemStore()
    with pytest.raises(ValueError):
        consume(store, send_hash="nope", request_key=REQ1)


def test_empty_request_key_raises():
    store = MemStore()
    with pytest.raises(ValueError):
        consume(store, send_hash=HASH, request_key="")


def test_canonical_key_differs_on_changed_body_and_method():
    assert canonical_request_key(method="POST", path="/p", body_include=["a"], body={"a": 1}) != canonical_request_key(
        method="POST", path="/p", body_include=["a"], body={"a": 2}
    )
    assert canonical_request_key(method="GET", path="/p") != canonical_request_key(method="POST", path="/p")
    # query included deterministically
    assert (
        canonical_request_key(method="GET", path="/p", query={"b": "2", "a": "1"})
        == canonical_request_key(method="GET", path="/p", query={"a": "1", "b": "2"})
    )
    # query_include filters which params enter the key
    k_all = canonical_request_key(method="GET", path="/p", query={"a": "1", "noise": "x"})
    k_filt = canonical_request_key(method="GET", path="/p", query={"a": "1", "noise": "x"}, query_include=["a"])
    assert k_all != k_filt
    assert k_filt == canonical_request_key(method="GET", path="/p", query={"a": "1"})