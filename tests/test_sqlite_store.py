"""Offline tests for the durable SQLite consumption store (atomicity, durability)."""

from __future__ import annotations

import pytest

from x402_nano_exact.consumption import ConcurrentBind, ConsumptionRow
from x402_nano_exact.sqlite_store import SqliteConsumptionStore

HASH = "C" * 64


def test_bind_complete_get_roundtrip(tmp_path):
    store = SqliteConsumptionStore(str(tmp_path / "c.db"))
    store.bind(ConsumptionRow(send_hash=HASH, request_key="k1"))
    assert store.get(HASH).request_key == "k1"
    store.complete(HASH, "200")
    row = store.get(HASH)
    assert row.status == "completed"
    assert row.response_handle == "200"
    assert row.outcome_known()


def test_bind_is_unique_and_concurrent_bind_raises(tmp_path):
    store = SqliteConsumptionStore(str(tmp_path / "c.db"))
    store.bind(ConsumptionRow(send_hash=HASH, request_key="k1"))
    with pytest.raises(ConcurrentBind):
        store.bind(ConsumptionRow(send_hash=HASH, request_key="k2"))


def test_durable_across_store_instances(tmp_path):
    path = str(tmp_path / "c.db")
    store = SqliteConsumptionStore(path)
    store.bind(ConsumptionRow(send_hash=HASH, request_key="k1"))
    store.complete(HASH, "200")
    # a fresh store on the same file must still see the committed row
    reopened = SqliteConsumptionStore(path)
    row = reopened.get(HASH)
    assert row is not None
    assert row.status == "completed"
    assert row.response_handle == "200"


def test_resolve_unknown_only_touches_pending(tmp_path):
    store = SqliteConsumptionStore(str(tmp_path / "c.db"))
    store.bind(ConsumptionRow(send_hash=HASH, request_key="k1"))
    store.resolve_unknown(HASH)
    assert store.get(HASH).status == "outcome_unknown"


def test_resolve_unknown_does_not_overwrite_completed(tmp_path):
    store = SqliteConsumptionStore(str(tmp_path / "c.db"))
    store.bind(ConsumptionRow(send_hash=HASH, request_key="k1"))
    store.complete(HASH, "200")
    store.resolve_unknown(HASH)
    assert store.get(HASH).status == "completed"


def test_bad_hash_rejected_at_bind(tmp_path):
    store = SqliteConsumptionStore(str(tmp_path / "c.db"))
    with pytest.raises(ValueError):
        store.bind(ConsumptionRow(send_hash="bad", request_key="k1"))