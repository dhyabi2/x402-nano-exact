"""HTTP JSON-RPC reader for a Nano node — a concrete ``NanoNode`` for mainnet.

``ConfirmationAuthority`` (confirmation.py) needs two reads from a node:

* is the exact send block `cemented` (not merely accepted into the pipeline)?
* what are its payee/amount facts?

This module implements those two reads as JSON-RPC calls against a Nano
node's RPC endpoint (default the public mainnet RPC at ``https://rpc.nano.to``).
It is read-only: it never signs, publishes or funds anything. The two RPC
methods used are ``block_info`` (with ``json_block: true``) for the block
facts and the confirmation status carried in its ``confirmed`` field, which a
quorum of network principal representatives cement. The node is the
*confirmation authority*; the quorum it syncs to is what we trust for
cementation.

Scope discipline ("source-only"): this reader is the only part that touches a
network. Everything above it (the authority decision, the consumption
binding) is pure and fully deterministic once the facts are supplied, so all
tests use a double and never hit the network.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from urllib import error, request

from .confirmation import BlockFacts, require_hex_64


class NanoRpcError(RuntimeError):
    """Raised when the Nano RPC node returns a transport or RPC error."""


def _post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    payload = json.dumps(body).encode()
    try:
        with request.urlopen(url, payload, timeout=timeout) as resp:  # noqa: S310 - http(s) node URL is caller-provided
            return json.loads(resp.read().decode())
    except (error.URLError, OSError, socket.timeout, json.JSONDecodeError) as exc:
        raise NanoRpcError(f"Nano RPC transport error: {exc}") from exc


class HttpNanoNode:
    """A ``NanoNode`` that reads an RPC node over HTTP JSON-RPC."""

    def __init__(self, rpc_url: str = "https://rpc.nano.to", timeout: float = 30.0) -> None:
        self._rpc_url = rpc_url
        self._timeout = timeout

    def is_cemented(self, block_hash: str) -> bool:
        require_hex_64(block_hash)
        result = self._call("block_info", {"json_block": "true", "hash": block_hash})
        confirmed = result.get("confirmed")
        return confirmed is True

    def block_facts(self, block_hash: str) -> BlockFacts:
        require_hex_64(block_hash)
        result = self._call("block_info", {"json_block": "true", "hash": block_hash})
        block = result.get("contents") or result.get("block") or {}
        return BlockFacts(
            link_as_account=block.get("link_as_account") or block.get("link"),
            amount=str(block.get("amount", "")) or None,
        )

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = _post(
            self._rpc_url,
            {"action": method, **params},
            timeout=self._timeout,
        )
        if "error" in result:
            raise NanoRpcError(f"Nano RPC {method} error: {result['error']}")
        return result