"""Mounted payment middleware plus the Nano guard. SPDX-License-Identifier: MIT"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
import uvicorn

from support import PAY_TO, build, events, new_hash, paid, request, reset

CHAT = b'{"prompt":"a"}'


@pytest.fixture
def server(tmp_path: Path):
    reset()
    seen: list[dict] = []
    app, _fac, _rpc = build(tmp_path / "consumption.db", seen)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    running = uvicorn.Server(config)
    threading.Thread(target=running.run, daemon=True).start()
    deadline = time.time() + 15
    while not running.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    port = running.servers[0].sockets[0].getsockname()[1]
    yield port, seen
    running.should_exit = True
    app.state.fac.shutdown()
    app.state.rpc.shutdown()


def _ops(send_hash: str) -> list[str]:
    return [event["op"] for event in events() if event.get("hash") == send_hash]


def test_health_has_no_facilitator_post(server) -> None:
    port, _seen = server
    before = len(events())
    status, _headers, body = request(port, "GET", "/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"
    assert events()[before:] == []


def test_unconfirmed_payment_does_not_run_the_handler(server) -> None:
    port, seen = server
    send_hash = new_hash()
    status, body = paid(port, "POST", "/api/chat", send_hash, CHAT, confirmed=False)
    assert status == 402
    assert b"not confirmed" in body
    assert seen == []
    assert "settle" not in _ops(send_hash)


def test_confirmed_body_is_visible_and_a_changed_prompt_is_refused(server) -> None:
    port, seen = server
    send_hash = new_hash()
    status, body = paid(port, "POST", "/api/chat", send_hash, b'{"prompt":"a"}')
    assert status == 200
    assert json.loads(body) == {"reply": "a", "route": "chat"}
    status, body = paid(port, "POST", "/api/chat", send_hash, b'{"prompt":"b"}')
    assert status == 409
    assert b"another request" in body
    assert [item["prompt"] for item in seen] == ["a"]


def test_declared_body_parameter_reads_the_prompt(server) -> None:
    port, seen = server
    status, body = paid(port, "POST", "/api/declared", new_hash(), b'{"prompt":"declared"}')
    assert status == 200
    assert json.loads(body)["reply"] == "declared"
    assert seen[-1]["prompt"] == "declared"


def test_retry_skips_the_handler_and_settles_again(server) -> None:
    port, seen = server
    send_hash = new_hash()
    status, body = paid(port, "POST", "/api/chat", send_hash, CHAT)
    assert status == 200
    status, body = paid(port, "POST", "/api/chat", send_hash, CHAT)
    assert status == 200
    assert json.loads(body) == {"result": "200", "replayed": True}
    assert len(seen) == 1
    settles = [event for event in events() if event["op"] == "settle" and event["hash"] == send_hash]
    assert len(settles) == 2


def test_repeated_query_is_distinct_from_the_single_value(server) -> None:
    port, seen = server
    send_hash = new_hash()
    status, _body = paid(port, "POST", "/api/chat?a=1&a=2", send_hash, CHAT)
    assert status == 200
    assert seen[-1]["items"] == [("a", "1"), ("a", "2")]
    status, body = paid(port, "POST", "/api/chat?a=2", send_hash, CHAT)
    assert status == 409
    assert b"another request" in body
    assert len(seen) == 1


def test_quote_uses_the_public_payee_string(server) -> None:
    port, _seen = server
    status, headers, _body = request(port, "POST", "/api/chat", headers={"Content-Type": "application/json"}, body=CHAT)
    assert status == 402
    from x402.http.utils import decode_payment_required_header

    header_map = {key.lower(): value for key, value in headers.items()}
    decoded = decode_payment_required_header(header_map["payment-required"])
    chosen = next(item for item in decoded.accepts if item.network == "nano:mainnet")
    assert chosen.pay_to == PAY_TO
    assert chosen.amount == str(10**28)
    assert chosen.asset == "XNO"


def test_guard_denies_non_loopback() -> None:
    with pytest.raises(OSError):
        socket.create_connection(("203.0.113.5", 9), timeout=1)
