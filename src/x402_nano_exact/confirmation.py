"""Confirmation authority for Nano (mainnet) x402 payments.

The x402 ``exact`` facilitator's ``settle`` path returns the send block's
hash as soon as the local node *accepted* it into the processing pipeline
(``processBlock``); it does not itself observe cementation (a confirmed
frontier roll-forward), and there is no request<->payment consumption binding
anywhere in the current ``@x402nano/exact`` / ``x402`` stack.

This module is the *resource-server side* of that missing guarantee: a small,
versioned, source-only authority that turns "a voucher that may still be
unconfirmed or already re-spent" into "the exact quoted block is cemented and
now consumed by exactly one protected request".

Scope discipline
----------------
* It is additive and thin. It does **not** replace the facilitator's
  ``verify``/``settle``; it sits in front of the protected handler and lifts
  the two guarantees the stack does not currently provide:
  1. confirmation (cementation) of the exact quoted send hash, and
  2. (see ``consumption.py``) single-use redemption bound to one request.
* It never signs, publishes or funds anything. It only *reads* a Nano RPC
  node (the confirmation authority) and a durable consumption store.
* The node client is injected; no network call is made inside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


def require_hex_64(hash_: str) -> None:
    """Require a Nano block hash: exactly 64 hex digits.

    A send hash is a 64-hex-digit transaction id on the Nano network. This
    authority pins to that canonical shape and never fabricates a digest from
    other fields, because binding a payment to an invented hash would be the
    exact reuse-rejection gap this middleware exists to close.
    """
    if not isinstance(hash_, str):
        raise TypeError(f"send hash must be a str, got {type(hash_).__name__}")
    if len(hash_) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hash_):
        raise ValueError(f"send hash must be 64 hex digits, got {hash_!r}")


@dataclass(frozen=True)
class BlockFacts:
    """Payee/amount facts of an opened Nano send block.

    ``link_as_account`` is the recipient (the payee). ``amount`` is the raw
    (10^30 per XNO) integer amount the block moves.
    """

    link_as_account: str | None
    amount: str | None

    def __bool__(self) -> bool:
        return bool(self.link_as_account and self.amount)


class NanoNode(Protocol):
    """What the confirmation authority needs from a Nano RPC node."""

    def is_cemented(self, block_hash: str) -> bool:
        """Return True only when ``block_hash`` is a cemented block on the node."""
        ...

    def block_facts(self, block_hash: str) -> BlockFacts:
        """Return the opened block's payee/amount facts, or an empty ``BlockFacts``."""
        ...


def normalize_pay_to(account: str) -> str:
    """Canonical ``nano_`` form of a Nano account for equality checks."""
    return "nano_" + account.removeprefix("nano_").removeprefix("xrb_")


@dataclass(frozen=True)
class ConfirmationCheck:
    """Result of observing a quoted send block against a confirmation authority."""

    confirmed: bool
    reason: str
    # Present only when confirmed: the eye-witness facts the quote required.
    confirmed_hash: str | None = None
    pay_to_matches: bool = False
    amount_matches: bool = False


class ConfirmationAuthority:
    """Decide whether a quoted send is cemented and matches the quoted quote.

    This is the "observe confirmation of the exact quoted send hash" control.
    The three things that must *all* be true before a payment may be consumed:

    * the exact quoted send hash is a cemented block (a ``process-accept``
      hash or a newer account frontier does not count), and
    * the cemented block's recipient equals the quoted ``payTo``, and
    * the cemented block's amount equals the quoted raw amount.

    Args:
        node: The confirmation authority (a Nano RPC node client).
    """

    def __init__(self, node: NanoNode) -> None:
        self._node = node

    def check(
        self,
        *,
        send_hash: str,
        quoted_pay_to: str,
        quoted_amount_raw: str,
    ) -> ConfirmationCheck:
        """Return whether ``send_hash`` is cemented and matches the quote."""
        try:
            require_hex_64(send_hash)
        except (TypeError, ValueError) as error:
            return ConfirmationCheck(confirmed=False, reason=str(error))

        if not self._node.is_cemented(send_hash):
            return ConfirmationCheck(
                confirmed=False,
                reason="send block is not confirmed (cemented) by the confirmation authority",
            )

        facts = self._node.block_facts(send_hash)
        if not facts:
            return ConfirmationCheck(
                confirmed=False,
                reason="cemented block facts (payee/amount) could not be read",
            )

        pay_to_matches = normalize_pay_to(facts.link_as_account) == normalize_pay_to(quoted_pay_to)
        amount_matches = str(facts.amount) == str(quoted_amount_raw)
        ok = pay_to_matches and amount_matches
        return ConfirmationCheck(
            confirmed=ok,
            reason="confirmed" if ok else "cemented send does not match the quoted payee/amount",
            confirmed_hash=send_hash,
            pay_to_matches=pay_to_matches,
            amount_matches=amount_matches,
        )