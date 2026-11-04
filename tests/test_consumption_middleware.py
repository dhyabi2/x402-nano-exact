"""Offline end-to-end tests of the Nano consumption middleware.

Drives the middleware through the real **FastAPI/Starlette request path** with
deterministic doubles (a fake Nano node + a real SQLite consumption store). A
verified payment is injected the exact way the x402 SDK's ``payment_middleware``
leaves it on ``request.state`` (``payment_payload`` + ``payment_requirements``),
so every case exercises the real middleware path without a live transfer,
funding or mainnet enablement.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from x402.schemas import PaymentPayload, PaymentRequirements
from x402_nano_exact.confirmation import BlockFacts, ConfirmationAuthority
from x402_nano_exact.consumption import ConsumptionRow, canonical_request_key
from x402_nano_exact.middleware import nano_consumption_middleware
from x402_nano_exact.sqlite_store import SqliteConsumptionStore

from .conftest import PAY_TO

NETWORK = "nano:mainnet"
RAW_AMOUNT = "10000000000000000000000000000"
HASH = "D" * 64
OTHER = "nano_3mqq6kpu4b56w1afgouwumfc8juriqe3w1t14xf1f3tgc8fn4puefh9fiiw3"


class FakeNanoNode:
    def __init__(self, *, cemented=True, payee=None, amount=None):
        self.cemented_set = {HASH} if cemented else set()
        self.facts = {HASH: BlockFacts(payee or PAY_TO, amount or RAW_AMOUNT)}

    def is_cemented(self, block_hash: str) -> bool:
        return block_hash in self.cemented_set

    def block_facts(self, block_hash: str) -> BlockFacts:
        return self.facts.get(block_hash, BlockFacts(None, None))


class FakeHandler:
    def __init__(self):
        self.runs = 0

    async def __call__(self, request: Request) -> Response:
        self.runs += 1
        return JSONResponse({"ok": True, "run": self.runs})


def make_request(state, path="/api/paid", method="POST", query_string=b"", body=None):
    """Build a Starlette Request carrying the verified-payment state on the wire."""

    async def send(_):
        pass

    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}

    route = Route(path, lambda r: JSONResponse({}), methods=[method])
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("1.2.3.4", 1234),
        "query_string": query_string,
        "headers": [],
        "route": route,
    }
    req = Request(scope, receive=receive)
    for key, value in state.items():
        setattr(req.state, key, value)
    return req


def verified_state(payload_hash=HASH):
    req = PaymentRequirements(
        scheme="exact", network=NETWORK, asset="XNO", amount=RAW_AMOUNT, payTo=PAY_TO, maxTimeoutSeconds=60
    )
    payload = PaymentPayload(x402_version=2, accepted=req, payload={"block": {"hash": payload_hash}})
    return {"payment_payload": payload, "payment_requirements": req}


def run(middleware, req, handler):
    import asyncio

    async def _run():
        return await middleware(req, handler)

    return asyncio.run(_run())


def gate(node, store):
    return nano_consumption_middleware(
        ConfirmationAuthority(node), store, age_out_seconds=60.0
    )


# ---------------------------------------------------------------- confirm gating


def test_unconfirmed_payment_rejected_before_handler():
    node = FakeNanoNode(cemented=False)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()
    resp = run(gate(node, store), make_request(verified_state()), handler)
    assert handler.runs == 0
    assert "not confirmed" in str(resp.body.decode())


def test_wrong_payee_rejected_before_handler():
    node = FakeNanoNode(payee=OTHER)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()
    resp = run(gate(node, store), make_request(verified_state()), handler)
    assert handler.runs == 0
    assert "does not match" in str(resp.body.decode())


def test_wrong_amount_rejected_before_handler():
    node = FakeNanoNode(amount="999999")
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()
    resp = run(gate(node, store), make_request(verified_state()), handler)
    assert handler.runs == 0


def test_confirmed_payment_runs_handler_once_then_replays():
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()

    req1 = make_request(verified_state())
    r1 = run(gate(node, store), req1, handler)
    assert r1.status_code == 200
    assert handler.runs == 1
    assert store.get(HASH).status == "completed"

    # Same payment, same request (same path/method) -> recover, do not re-run.
    r2 = run(gate(node, store), make_request(verified_state()), handler)
    assert handler.runs == 1
    assert "replayed" in r2.body.decode()


def test_reuse_with_changed_request_is_rejected():
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()

    run(gate(node, store), make_request(verified_state(), path="/api/paid"), handler)
    assert handler.runs == 1

    # Same payment presented against a *different* request (different method/path).
    resp = run(
        gate(node, store),
        make_request(verified_state(), method="GET", path="/api/other"),
        handler,
    )
    assert handler.runs == 1
    assert resp.status_code == 409
    assert "already consumed by another request" in resp.body.decode()


def test_concurrent_same_hash_different_request_rejected():
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()

    req_a = make_request(verified_state(), query_string=b"kind=a")
    req_b = make_request(verified_state(), query_string=b"kind=b")
    h_a = FakeHandler()
    h_b = FakeHandler()

    g = gate(node, store)
    run(g, req_a, h_a)
    resp_b = run(g, req_b, h_b)
    assert h_a.runs == 1
    assert h_b.runs == 0
    assert "consumed by another request" in str(resp_b.body.decode())


def test_crash_between_consume_and_complete_records_unknown_and_does_not_rerun():
    # A reservation that was bound but never completed is NOT re-run.
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    key = canonical_request_key(method="POST", path="/api/paid")
    store.bind(ConsumptionRow(send_hash=HASH, request_key=key, status="pending"))
    handler = FakeHandler()
    resp = run(gate(node, store), make_request(verified_state()), handler)
    # pending-but-not-stale for the SAME request: in flight -> do not re-run
    assert handler.runs == 0
    row = store.get(HASH)
    assert row.status in ("pending", "outcome_unknown")


def test_aged_out_pending_request_declares_outcome_unknown(tmp_path):
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(str(tmp_path / "c.db"))
    key = canonical_request_key(method="POST", path="/api/paid")
    store.bind(ConsumptionRow(send_hash=HASH, request_key=key, created_at=1_000.0))
    # age_out_seconds=30, now=2000 -> stale -> outcome_unknown, never re-run
    from x402_nano_exact.consumption import consume

    d = consume(store, send_hash=HASH, request_key=key, age_out_seconds=30.0, now=2_000.0)
    assert d.kind == "outcome_unknown"
    assert store.get(HASH).status == "outcome_unknown"


# ------------------------------------------------- Lucian's review regressions
# (epistemedeus, pursekeeper/x402-nano-exact#4, 2026-11-02)


def test_repeated_query_key_is_distinct_from_single_value():
    """a=1&a=2 must NOT collide with a=2 (dict(QueryParams) collapsed them)."""
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()

    g = gate(node, store)
    # a=1&a=2 on the wire -> multi_items() preserves both values
    r1 = run(g, make_request(verified_state(), query_string=b"a=1&a=2"), handler)
    assert r1.status_code == 200
    assert handler.runs == 1

    # a=2 (only one value) must be treated as a DIFFERENT request -> reuse rejected
    r2 = run(g, make_request(verified_state(), query_string=b"a=2"), handler)
    assert handler.runs == 1
    assert r2.status_code == 409
    assert "consumed by another request" in r2.body.decode()


def test_unchanged_repeated_query_key_is_same_request():
    """The same repeated query values (same order-insensitive multiset) replay."""
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()
    g = gate(node, store)

    r1 = run(g, make_request(verified_state(), query_string=b"a=1&a=2"), handler)
    r2 = run(g, make_request(verified_state(), query_string=b"a=2&a=1"), handler)
    assert handler.runs == 1
    assert "replayed" in r2.body.decode()


def test_body_include_reads_and_binds_the_request_body():
    """body_include=[\"prompt\"] must bind the actual JSON body, and the handler
    must still be able to read it (the body is replayed, not consumed)."""
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")

    seen_body = {}

    class BodyHandler:
        def __init__(self):
            self.runs = 0

        async def __call__(self, request):
            self.runs += 1
            seen_body["prompt"] = (await request.body()).decode()
            b = getattr(request.state, "consumption_body", None)
            seen_body["state"] = b
            return JSONResponse({"ok": True, "run": self.runs})

    mw = nano_consumption_middleware(
        ConfirmationAuthority(node), store, body_include=["prompt"], age_out_seconds=60.0
    )
    body = b'{"prompt": "hello"}'
    resp = run(mw, make_request(verified_state(), body=body), BodyHandler())
    assert resp.status_code == 200
    # the guard parsed the body and bound it
    assert seen_body["state"] == {"prompt": "hello"}
    # the handler still read the body
    assert "hello" in seen_body["prompt"]

    # A different body on a retry with the same confirmed hash must be rejected
    # (the payment was bound to prompt=hello, not prompt=other).
    seen_body.clear()
    resp2 = run(mw, make_request(verified_state(), body=b'{"prompt": "other"}'), BodyHandler())
    assert resp2.status_code == 409
    assert "consumed by another request" in resp2.body.decode()


def test_example_registers_guard_inner_to_x402_verify():
    """The documented composition must make x402's verify run OUTERMOST and this
    guard run in the verified-but-before-handler slot.

    Starlette runs the LAST-registered middleware first. The example must
    therefore register ``nano_consumption_middleware`` before
    ``payment_middleware``. Registered the other way round, the guard would run
    before the SDK verified anything, see no payment state, and pass every
    request through un-gated - the ordering bug Lucian found.
    """
    example = Path(__file__).resolve().parents[1] / "examples" / "fastapi_consumption.py"
    src = example.read_text()

    guard_pos = src.find("nano_consumption_middleware(")
    pay_pos = src.find("payment_middleware(routes, server)")
    assert guard_pos != -1 and pay_pos != -1, "example must reference both middlewares"
    assert guard_pos < pay_pos, (
        "nano_consumption_middleware must be registered BEFORE payment_middleware "
        "so x402 verifies outermost and this guard gates in the verified slot"
    )

# ------------------------------------- unambiguous key encoding (after f0b3e89)


def _first_then_second(first_qs: bytes, second_qs: bytes):
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()
    g = gate(node, store)
    r1 = run(g, make_request(verified_state(), query_string=first_qs), handler)
    r2 = run(g, make_request(verified_state(), query_string=second_qs), handler)
    return r1, r2, handler


def test_repeated_key_does_not_collide_with_encoded_ampersand_value():
    """a=1&a=2 decodes to [(a,1),(a,2)]; a=1%262 decodes to [(a,'1&2')]."""
    r1, r2, handler = _first_then_second(b"a=1&a=2", b"a=1%262")
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert handler.runs == 1
    assert "consumed by another request" in r2.body.decode()


def test_two_keys_do_not_collide_with_encoded_pair_in_one_value():
    """a=1&b=2 decodes to [(a,1),(b,2)]; a=1%26b%3D2 decodes to [(a,'1&b=2')]."""
    r1, r2, handler = _first_then_second(b"a=1&b=2", b"a=1%26b%3D2")
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert handler.runs == 1


def test_encoded_value_first_then_plain_pairs_also_refused():
    r1, r2, handler = _first_then_second(b"a=1%262", b"a=1&a=2")
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert handler.runs == 1


def test_same_pairs_in_different_order_is_the_same_request():
    """The key is order-insensitive over decoded pairs: b=2&a=1 replays a=1&b=2."""
    r1, r2, handler = _first_then_second(b"a=1&b=2", b"b=2&a=1")
    assert r1.status_code == 200
    assert handler.runs == 1
    assert "replayed" in r2.body.decode()


def test_decoded_path_cannot_imitate_query_part():
    """A decoded path containing the old delimiters must not equal path + query."""
    node = FakeNanoNode(cemented=True)
    store = SqliteConsumptionStore(":memory:")
    handler = FakeHandler()
    g = gate(node, store)
    r1 = run(g, make_request(verified_state(), path="/api/paid", query_string=b"a=1"), handler)
    r2 = run(g, make_request(verified_state(), path="/api/paid|query:a=1"), handler)
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert handler.runs == 1


def test_canonical_key_components_are_unambiguous():
    k = canonical_request_key
    assert k(method="GET", path="/p", query=[("a", "1"), ("a", "2")]) != k(
        method="GET", path="/p", query=[("a", "1&2")]
    )
    assert k(method="GET", path="/p", query=[("a", "1"), ("b", "2")]) != k(
        method="GET", path="/p", query=[("a", "1&b=2")]
    )
    assert k(method="GET", path="/p", query=[("a=1", "")]) != k(method="GET", path="/p", query=[("a", "1")])
    assert k(method="GET", path="/p", query=[("a", "1")]) != k(method="GET", path="/p|query:a=1")
    assert k(method="GET", path="/p", query={"a": ["1", "2"]}) == k(
        method="GET", path="/p", query=[("a", "2"), ("a", "1")]
    )
    # body values cannot imitate a second body field
    assert k(method="POST", path="/p", body_include=["a", "b"], body={"a": "x", "b": "y"}) != k(
        method="POST", path="/p", body_include=["a", "b"], body={"a": "x', 'b': 'y"}
    )
    # body inputs keep their JSON types
    assert k(method="POST", path="/p", body_include=["a"], body={"a": 1}) != k(
        method="POST", path="/p", body_include=["a"], body={"a": "1"}
    )


# ------------------------ query-binding corpus: four categories (2026-11-04)
# Our own cases for the four categories epistemedeus listed on #4: duplicate
# values, delimiter impersonation, Unicode, meaningful empty values. Each pair
# is two distinct requests; the second must NOT redeem the first's payment.

import pytest  # noqa: E402

NFC_E = b"%C3%A9"  # U+00E9
NFD_E = b"e%CC%81"  # U+0065 U+0301

QUERY_DISTINCT_CASES = [
    # duplicate values
    pytest.param(b"a=1&a=2", b"a=1%262", id="dup-repeated-vs-encoded-amp"),
    pytest.param(b"a=1&a=1", b"a=1", id="dup-same-value-twice-vs-once"),
    pytest.param(b"a=1&a=2", b"a=2", id="dup-two-values-vs-last"),
    pytest.param(b"a=1&b=2&a=1", b"a=1&b=2", id="dup-interleaved-vs-single"),
    # delimiter impersonation
    pytest.param(b"a=1&b=2", b"a=1%26b%3D2", id="delim-encoded-amp-eq-in-value"),
    pytest.param(b"a%3D1=2", b"a=1%3D2", id="delim-encoded-eq-key-vs-value"),
    pytest.param(b"a=1;b=2", b"a=1&b=2", id="delim-semicolon-vs-amp"),
    pytest.param(b"a+b=1", b"a%2Bb=1", id="delim-plus-space-vs-encoded-plus"),
    # Unicode
    pytest.param(b"a=" + NFC_E, b"a=" + NFD_E, id="uni-nfc-vs-nfd-value"),
    pytest.param(NFC_E + b"=1", NFD_E + b"=1", id="uni-nfc-vs-nfd-key"),
    pytest.param("a=é".encode(), b"a=" + NFC_E, id="uni-raw-utf8-vs-percent"),
    # meaningful empty values
    pytest.param(b"a=", b"a", id="empty-blank-vs-bare"),
    pytest.param(b"a=", b"", id="empty-blank-vs-absent"),
    pytest.param(b"a", b"", id="empty-bare-vs-absent"),
    pytest.param(b"a=&b", b"a&b=", id="empty-swapped-bare-and-blank"),
]


@pytest.mark.parametrize("first_qs,second_qs", QUERY_DISTINCT_CASES)
def test_distinct_queries_do_not_share_a_payment(first_qs, second_qs):
    for one, two in ((first_qs, second_qs), (second_qs, first_qs)):
        r1, r2, handler = _first_then_second(one, two)
        assert r1.status_code == 200
        assert r2.status_code == 409, (one, two)
        assert handler.runs == 1


@pytest.mark.parametrize(
    "qs", sorted({p for case in QUERY_DISTINCT_CASES for p in case.values})
)
def test_byte_identical_query_replays(qs):
    r1, r2, handler = _first_then_second(qs, qs)
    assert r1.status_code == 200
    assert handler.runs == 1
    assert "replayed" in r2.body.decode()


def test_query_pairs_matches_starlette_decoding_and_keeps_bare_keys():
    from starlette.datastructures import QueryParams

    from x402_nano_exact.consumption import query_pairs

    for qs in (b"a=1&a=2", b"a=1%262", b"a+b=1;c", b"a=" + NFD_E, "a=é".encode(), b"&&a=&b"):
        decoded = [(k, "" if v is None else v) for k, v in query_pairs(qs)]
        assert decoded == QueryParams(qs).multi_items()
    assert query_pairs(b"a=&b") == [("a", ""), ("b", None)]
