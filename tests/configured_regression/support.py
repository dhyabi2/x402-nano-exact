"""Loopback doubles for the configured regression. SPDX-License-Identifier: MIT"""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PAY_TO = "nano_3mzq6kpu4b56w1afgouwumfc8juriqe3w1t14xf1f3tgc8fn4puepfrjhcfk"
_SEQ = itertools.count(1)
_INSTALLED = False


def new_hash() -> str:
    return f"{next(_SEQ):064x}"


def install_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    allowed = {"127.0.0.1", "::1", "localhost", ""}
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def _note(kind: str, host: str) -> None:
        path = os.environ.get("CONFIGURED_ATTEMPT_LOG")
        if not path:
            return
        line = json.dumps({"kind": kind, "host": host}) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)

    def _ok(host: str) -> bool:
        return host in allowed or host.startswith("127.")

    def connect(self, address):  # type: ignore[no-untyped-def]
        host = str(address[0]) if isinstance(address, tuple) and address else str(address)
        _note("connect", host)
        if not _ok(host):
            raise OSError(f"outbound denied: {address}")
        return real_connect(self, address)

    def getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        host_s = "" if host is None else str(host)
        _note("dns", host_s)
        if host_s and not _ok(host_s):
            raise socket.gaierror(f"dns denied: {host_s}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


install_guard()

from fastapi import FastAPI, Request
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.utils import decode_payment_required_header, encode_payment_signature_header
from x402.schemas import PaymentPayload

from x402_nano_exact import ExactNanoServerScheme
from x402_nano_exact.confirmation import ConfirmationAuthority
from x402_nano_exact.middleware import nano_consumption_middleware
from x402_nano_exact.rpc_node import HttpNanoNode
from x402_nano_exact.sqlite_store import SqliteConsumptionStore

_EVENTS: list[dict[str, Any]] = []
_BLOCKS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def reset() -> None:
    with _LOCK:
        _EVENTS.clear()
        _BLOCKS.clear()


def events() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_EVENTS)


def set_block(send_hash: str, facts: dict[str, Any]) -> None:
    with _LOCK:
        _BLOCKS[send_hash] = dict(facts)


class _Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode())
        return data if isinstance(data, dict) else {}

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Facilitator(_Quiet):
    def do_GET(self) -> None:  # noqa: N802
        self._send(200, {"kinds": [{"x402Version": 2, "scheme": "exact", "network": "nano:mainnet", "extra": {"asset": "XNO", "work": "required", "workThreshold": "fffffff800000000"}}], "extensions": [], "signers": {}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._json()
        payload = body.get("paymentPayload") or {}
        inner = payload.get("payload") if isinstance(payload, dict) else {}
        block = inner.get("block") if isinstance(inner, dict) else {}
        send_hash = block.get("hash") if isinstance(block, dict) else None
        with _LOCK:
            _EVENTS.append({"op": path.strip("/"), "hash": send_hash})
        if path == "/verify":
            self._send(200, {"isValid": True, "payer": PAY_TO})
            return
        if path == "/settle":
            self._send(200, {"success": True, "transaction": "lab-not-broadcast", "network": "nano:mainnet", "payer": PAY_TO})
            return
        self._send(404, {"error": "not found"})


class Rpc(_Quiet):
    def do_POST(self) -> None:  # noqa: N802
        body = self._json()
        send_hash = body.get("hash")
        with _LOCK:
            _EVENTS.append({"op": "rpc", "hash": send_hash})
            facts = dict(_BLOCKS.get(send_hash, {}))
        contents = {}
        if "link_as_account" in facts:
            contents["link_as_account"] = facts["link_as_account"]
        if "amount" in facts:
            contents["amount"] = facts["amount"]
        raw = json.dumps({"confirmed": facts.get("confirmed", False), "contents": contents, "subtype": facts.get("subtype", "send")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def build(db: Path, seen: list[dict[str, Any]]) -> tuple[FastAPI, str, str]:
    fac = _serve(Facilitator)
    rpc = _serve(Rpc)
    fac_url = f"http://127.0.0.1:{fac.server_address[1]}"
    rpc_url = f"http://127.0.0.1:{rpc.server_address[1]}"
    server = x402ResourceServer([HTTPFacilitatorClient(FacilitatorConfig(url=fac_url, timeout=5.0))])
    server.register("nano:mainnet", ExactNanoServerScheme())
    server.initialize()
    accept = {"scheme": "exact", "network": "nano:mainnet", "price": "0.01", "payTo": PAY_TO, "maxTimeoutSeconds": 60}
    routes = {
        "POST /api/chat": {"accepts": [accept]},
        "POST /api/declared": {"accepts": [dict(accept)]},
    }
    payment = payment_middleware(routes, server, sync_facilitator_on_start=True)
    guard = nano_consumption_middleware(
        ConfirmationAuthority(HttpNanoNode(rpc_url)),
        SqliteConsumptionStore(str(db)),
        body_include=["prompt"],
    )
    app = FastAPI()
    app.middleware("http")(guard)
    app.middleware("http")(payment)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/chat")
    async def chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        prompt = body.get("prompt") if isinstance(body, dict) else None
        seen.append({"route": "chat", "prompt": prompt, "items": list(request.query_params.multi_items())})
        return {"reply": prompt, "route": "chat"}

    @app.post("/api/declared")
    async def declared(body: dict) -> dict[str, Any]:
        prompt = body.get("prompt")
        seen.append({"route": "declared", "prompt": prompt})
        return {"reply": prompt, "route": "declared"}

    app.state.fac = fac
    app.state.rpc = rpc
    return app, fac_url, rpc_url


def request(port: int, method: str, target: str, *, headers: dict[str, str] | None = None, body: bytes = b""):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, dict(resp.getheaders()), payload
    finally:
        conn.close()


def paid(port: int, method: str, target: str, send_hash: str, body: bytes, *, confirmed: bool = True) -> tuple[int, bytes]:
    status, headers, payload = request(port, method, target, headers={"Content-Type": "application/json"}, body=body)
    if status != 402:
        raise AssertionError(payload[:300])
    header_map = {key.lower(): value for key, value in headers.items()}
    decoded = decode_payment_required_header(header_map["payment-required"])
    chosen = next(item for item in decoded.accepts if item.network == "nano:mainnet")
    set_block(send_hash, {
        "confirmed": confirmed,
        "subtype": "send",
        "link_as_account": chosen.pay_to,
        "amount": chosen.amount,
    })
    wire = PaymentPayload(x402_version=2, accepted=chosen, payload={"block": {"hash": send_hash}})
    status, _headers, payload = request(
        port,
        method,
        target,
        headers={
            "Content-Type": "application/json",
            "PAYMENT-SIGNATURE": encode_payment_signature_header(wire),
        },
        body=body,
    )
    return status, payload
