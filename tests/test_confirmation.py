"""Offline tests for the confirmation authority (cementation + quote match)."""

from __future__ import annotations

import pytest

from x402_nano_exact.confirmation import (
    BlockFacts,
    ConfirmationAuthority,
    normalize_pay_to,
    require_hex_64,
)

from .conftest import PAY_TO

HASH = "A" * 64
RAW_AMOUNT = "10000000000000000000000000000"


class FakeNode:
    """Deterministic Nano node double: cementation set + one opened block."""

    def __init__(self, *, cemented: set[str], block_facts: dict[str, BlockFacts]) -> None:
        self.cemented = cemented
        self.facts = block_facts

    def is_cemented(self, block_hash: str) -> bool:
        return block_hash in self.cemented

    def block_facts(self, block_hash: str) -> BlockFacts:
        return self.facts.get(block_hash, BlockFacts(None, None))


def authoritynode(*, cemented=True, payee=None, amount=None) -> FakeNode:
    return FakeNode(
        cemented={HASH} if cemented else set(),
        block_facts={HASH: BlockFacts(payee or PAY_TO, amount or RAW_AMOUNT)},
    )


def authority(node: FakeNode) -> ConfirmationAuthority:
    return ConfirmationAuthority(node)


def test_cemented_matching_quote_is_confirmed():
    check = authority(authoritynode()).check(
        send_hash=HASH, quoted_pay_to=PAY_TO, quoted_amount_raw=RAW_AMOUNT
    )
    assert check.confirmed is True
    assert check.reason == "confirmed"
    assert check.confirmed_hash == HASH
    assert check.pay_to_matches and check.amount_matches


def test_uncemented_hash_is_not_confirmed():
    # A process-accept hash that never cemented must not be confirmed.
    check = authority(authoritynode(cemented=False)).check(
        send_hash=HASH, quoted_pay_to=PAY_TO, quoted_amount_raw=RAW_AMOUNT
    )
    assert check.confirmed is False
    assert "not confirmed" in check.reason


OTHER_PAYEE = "nano_3mqq6kpu4b56w1afgouwumfc8juriqe3w1t14xf1f3tgc8fn4puefh9fiiw3"


def test_wrong_payee_is_not_confirmed():
    check = authority(authoritynode(payee=OTHER_PAYEE)).check(
        send_hash=HASH, quoted_pay_to=PAY_TO, quoted_amount_raw=RAW_AMOUNT
    )
    assert check.confirmed is False
    assert "does not match" in check.reason
    assert check.pay_to_matches is False


def test_wrong_amount_is_not_confirmed():
    check = authority(authoritynode(amount="999999")).check(
        send_hash=HASH, quoted_pay_to=PAY_TO, quoted_amount_raw=RAW_AMOUNT
    )
    assert check.confirmed is False
    assert check.amount_matches is False


def test_non_hex_send_hash_is_rejected_not_confirmed():
    for bad in ("abc", "A" * 63, "A" * 65, "zz" * 32, ""):
        check = authority(authoritynode()).check(
            send_hash=bad, quoted_pay_to=PAY_TO, quoted_amount_raw=RAW_AMOUNT
        )
        assert check.confirmed is False


def test_pay_to_normalization_eq():
    assert normalize_pay_to("xrb_" + PAY_TO[5:]) == PAY_TO
    assert normalize_pay_to(PAY_TO) == PAY_TO


def test_require_hex_64():
    require_hex_64("ABcd1234" * 8)
    with pytest.raises(ValueError):
        require_hex_64("short")
    with pytest.raises(TypeError):
        require_hex_64(123)


def test_http_node_cemented_and_facts(monkeypatch):
    from x402_nano_exact.rpc_node import HttpNanoNode

    calls = []

    def fake_post(url, body, timeout):
        calls.append(body)
        if body["action"] == "block_info":
            return {
                "confirmed": True,
                "contents": {"link_as_account": PAY_TO, "amount": RAW_AMOUNT},
            }
        return {}

    monkeypatch.setattr("x402_nano_exact.rpc_node._post", fake_post)
    node = HttpNanoNode(rpc_url="http://fake-test")
    assert node.is_cemented(HASH) is True
    facts = node.block_facts(HASH)
    assert facts.link_as_account == PAY_TO
    assert facts.amount == RAW_AMOUNT
    assert [c["action"] for c in calls] == ["block_info", "block_info"]


def test_http_node_not_cemented_and_rpc_error(monkeypatch):
    from x402_nano_exact.rpc_node import HttpNanoNode, NanoRpcError

    monkeypatch.setattr(
        "x402_nano_exact.rpc_node._post",
        lambda url, body, timeout: {"confirmed": False, "contents": {}},
    )
    node = HttpNanoNode(rpc_url="http://fake-test")
    assert node.is_cemented(HASH) is False

    monkeypatch.setattr(
        "x402_nano_exact.rpc_node._post",
        lambda url, body, timeout: {"error": "Bad hash"},
    )
    with pytest.raises(NanoRpcError):
        node.block_facts(HASH)