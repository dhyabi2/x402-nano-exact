"""Consumption binding: one confirmed payment, consumed by exactly one request.

This closes the request<->payment consumption gap Lucian identified on
epistemedeus/x402-url-extractor#189: nothing in the current stack records
*which* protected request redeemed a confirmed send, rejects the same send
presented against a *different* request, or makes a retry of the *same*
request recover its stored outcome instead of running the handler twice.

Design
------
A **durable consumption store** is keyed by the canonical 64-hex send hash.
For each send hash it holds one atomic row:

    request_key  the canonical protected request that redeemed it
                 (method + decoded path + normalized query + a chosen set of
                 body/query inputs) - *not* a caller-chosen request id alone.
    status       pending | completed(response_handle) | outcome_unknown
    created_at   wall clock, so a crashed in-flight row can age out

Three guarantees, all enforced atomically in ``consume``:

* **Single-use under concurrency.** A compare-and-set reserves the send hash
  for the first request that binds to it; a concurrent request that presents
  the same hash is rejected. A retry of the *same* (request_key-matching)
  retry recovers the stored outcome instead of re-running the handler.
* **Reuse rejected.** Presenting the same confirmed hash with a *different*
  request_key is rejected with ``ReuseRejected``.
* **Crash safety.** If the process dies after consuming but before the handler
  outcome is recorded, the row stays ``pending`` and ages out to
  ``outcome_unknown``. A later request whose outcome is unknown is NOT allowed
  to re-run the handler's side effects or be told the server "atomically"
  served it; it gets a stable ``OutcomeUnknown`` response instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from .confirmation import require_hex_64


@dataclass
class ConsumptionRow:
    """One durable consumption decision for a confirmed send hash."""

    send_hash: str
    request_key: str
    status: str = "pending"  # pending | completed | outcome_unknown
    response_handle: str | None = None
    created_at: float = field(default_factory=time.time)

    def outcome_known(self) -> bool:
        return self.status == "completed" and self.response_handle is not None


class ConsumptionStore(Protocol):
    """A durable, atomic, single-writer store for consumption rows.

    Implementations MUST guarantee that ``bind`` and ``complete``/``resolve``
    are atomic with respect to each other (compare-and-set / row insert with a
    unique constraint), so two concurrent requests can never both win the
    single-use reservation.
    """

    def bind(self, row: ConsumptionRow) -> ConsumptionRow:
        """Reserve ``row.send_hash`` for ``row.request_key``.

        Returns the row that is now authoritative: the newly-created row when
        ``send_hash`` was free, or the existing row when it was already bound.

        Raises ``ConcurrentBind`` if the hash is already bound to a *different*
        request_key (the reuse-rejection path).
        """
        ...

    def complete(self, send_hash: str, response_handle: str) -> ConsumptionRow:
        """Record that ``send_hash``'s handler completed with a stored outcome."""
        ...

    def resolve_unknown(self, send_hash: str) -> None:
        """Mark an aged-out pending row as ``outcome_unknown``."""
        ...

    def get(self, send_hash: str) -> ConsumptionRow | None:
        """Return the row for ``send_hash`` (best-effort read)."""
        ...


class ConcurrentBind(RuntimeError):
    """The send hash is already bound to a different request."""


@dataclass(frozen=True)
class ConsumeDecision:
    """What the middleware should do with a confirmed, bound send hash."""

    kind: str  # proceed | replay_completed | outcome_unknown | reuse_rejected
    response_handle: str | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Decision logic (pure, fully testable)
# ---------------------------------------------------------------------------


def consume(
    store: ConsumptionStore,
    *,
    send_hash: str,
    request_key: str,
    age_out_seconds: float = 300.0,
    now: float | None = None,
) -> ConsumeDecision:
    """Bind a confirmed send hash to one request and return the decision.

    Args:
        store: The durable single-writer store.
        send_hash: The confirmed 64-hex send hash.
        request_key: Canonical key of the protected request redeeming it.
        age_out_seconds: How long a ``pending`` (handler outcome unknown)
            reservation may sit before it is declared ``outcome_unknown``.
        now: Injectable clock for deterministic tests.

    Returns:
        A ``ConsumeDecision``. On ``proceed`` the middleware must run the
        handler then call ``complete``. On ``replay_completed``/``outcome_unknown``/
        ``reuse_rejected`` the middleware must NOT re-run the handler.
    """
    require_hex_64(send_hash)
    if not request_key:
        raise ValueError("request_key is required to bind a consumption")

    existing = store.get(send_hash)
    if existing is None:
        try:
            bound = store.bind(
                ConsumptionRow(send_hash=send_hash, request_key=request_key)
            )
        except ConcurrentBind:
            # Another request won the reservation between our read and bind.
            # Re-read to decide fairly by request_key (not by arrival order).
            winner = store.get(send_hash)
            if winner is not None and winner.request_key == request_key:
                existing = winner
            else:
                existing = winner or ConsumptionRow(
                    send_hash=send_hash, request_key="", status="pending"
                )
                return ConsumeDecision(
                    kind="reuse_rejected",
                    reason="payment already consumed by another request",
                )
        else:
            return ConsumeDecision(kind="proceed", reason="reserved for this request")

    # Row already exists: either this exact request retry, a concurrent
    # different request, or a crashed / aged-out reservation.
    stale = now is not None and existing.created_at + age_out_seconds < now
    if stale and existing.status == "pending":
        store.resolve_unknown(send_hash)
        existing = store.get(send_hash)

    if existing.request_key != request_key:
        return ConsumeDecision(kind="reuse_rejected", reason="payment already consumed by another request")

    if existing.status == "outcome_unknown":
        return ConsumeDecision(kind="outcome_unknown", reason="prior handler outcome is unknown")

    if existing.status == "completed":
        return ConsumeDecision(
            kind="replay_completed",
            response_handle=existing.response_handle,
            reason="same request retry; returning stored outcome",
        )

    # pending for the SAME request key but not stale yet = concurrent/live:
    # do not re-run the handler, do not fabricate an outcome.
    return ConsumeDecision(
        kind="outcome_unknown",
        reason="payment still in flight for this request; outcome not yet known",
    )


# ---------------------------------------------------------------------------
# Canonical protected-request key
# ---------------------------------------------------------------------------


def canonical_request_key(
    *,
    method: str,
    path: str,
    query: dict[str, str | list[str]] | list[tuple[str, str]] | None = None,
    query_include: list[str] | None = None,
    body_include: list[str] | None = None,
    body: dict | None = None,
) -> str:
    """Build a canonical key for the protected request.

    Binds redemption to the *request itself* (method, decoded path, a chosen
    set of query params, and a caller-chosen set of body inputs), never to a
    caller-chosen request id alone, so the same payment cannot be re-used
    against a changed request body the payer never bought.

    ``query`` may be a ``{key: value}`` dict or a sequence of ``(key, value)``
    pairs (e.g. ``list(request.query_params.multi_items())``). Multi-value form
    is preferred: ``dict(QueryParams)`` collapses repeated keys (``a=1&a=2``
    becomes ``{'a': '2'}`` and would collide with ``a=2``), whereas the pair
    form keeps every value distinct so ``a=1&a=2`` and ``a=2`` differ.
    """
    parts = [method.upper(), path]
    if query:
        if isinstance(query, dict):
            pairs = list(query.items())
        else:
            pairs = list(query)
        # Group every (key, value) pair per key, preserving all values so a
        # repeated query key (a=1&a=2) stays distinguishable from a=2.
        grouped: dict[str, list[str]] = {}
        for k, v in pairs:
            if query_include is not None and k not in query_include:
                continue
            grouped.setdefault(k, []).append(str(v))
        normalized = sorted(
            f"{k}={'&'.join(sorted(v))}" for k, v in sorted(grouped.items())
        )
        parts.append("query:" + "&".join(normalized))
    if body_include:
        picked = {k: body.get(k) for k in body_include if isinstance(body, dict) and k in body}
        parts.append("body:" + repr(sorted(picked.items())))
    return "|".join(parts)