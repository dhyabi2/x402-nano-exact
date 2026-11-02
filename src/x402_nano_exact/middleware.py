"""FastAPI middleware that adds confirmation + single-use consumption control.

Compose it around the SDK's own ``payment_middleware`` (which does the
``verify -> handler -> settle`` flow). Middleware ordering matters:

    app.middleware("http")(payment_middleware(routes, server))          # outer
    app.middleware("http")(nano_consumption_middleware(guard, store))   # inner

Starlette runs the outermost middleware first and each middleware's
``call_next`` penetrates inward to the endpoint handler. So this middleware,
registered *after* x402's, runs in the verified-but-before-handler slot: it
reads ``request.state.payment_payload`` / ``payment_requirements`` (which x402
sets when a payment is verified) and, for ``nano:mainnet``/``exact``, enforces

1. confirmation of the exact quoted send hash (cementation + matching quote),
2. single-use consumption bound to the canonical protected request, and
3. crash-safe outcome handling (a retry of the same request recovers the
   stored outcome; a different request reusing the payment is rejected).

If the route is not a Nano x402 route (or the SDK did not verify anything),
the middleware passes straight through: it only ever gates what is already
concretely a verified nano:mainnet exact payment.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

try:
    from fastapi import Request, Response
    from fastapi.responses import JSONResponse
except ImportError as e:  # pragma: no cover - import test covers it
    raise ImportError(
        "nano consumption middleware requires fastapi. Install with: pip install 'x402[fastapi]'"
    ) from e

from .confirmation import ConfirmationAuthority, ConfirmationCheck
from .consumption import (
    ConsumptionStore,
    canonical_request_key,
    consume,
)

if TYPE_CHECKING:
    from .consumption import ConsumeDecision

logger = logging.getLogger(__name__)

NANO_NETWORK = "nano:mainnet"
EXACT_SCHEME = "exact"


def _send_hash_from_payload(payload: Any) -> str | None:
    """Extract the canonical 64-hex send hash from a verified payment payload.

    The ``exact``/``nano`` payload carries the signed send block; the hash on
    the wire (``payload.block.hash``, or ``payload.hash``) is what the
    facilitator's settle returned. Nothing here fabricates a hash — if the
    payer or the SDK did not supply one we refuse to consume.

    ``payload`` may be a :class:`PaymentPayload` (from ``request.state``) or
    its ``payload`` dict; ``accepted`` holds the quote.
    """
    if payload is None:
        return None
    # PaymentPayload object -> its free-form scheme dict
    wire = getattr(payload, "payload", None)
    if isinstance(wire, str):  # defensive: tolerate a JSON string
        import json as _json

        try:
            wire = _json.loads(wire)
        except Exception:
            wire = None
    if not isinstance(wire, dict):
        # allow the bare dict form too (tests / direct consumers)
        wire = payload if isinstance(payload, dict) else None
    if not isinstance(wire, dict):
        return None
    block = wire.get("block")
    if isinstance(block, dict):
        candidate = block.get("hash")
        if isinstance(candidate, str) and len(candidate) == 64:
            return candidate
    candidate = wire.get("hash")
    if isinstance(candidate, str) and len(candidate) == 64:
        return candidate
    return None


class NanoConsumptionMiddleware:
    """Confirm + single-use-bind a verified Nano x402 payment before the handler."""

    def __init__(
        self,
        authority: ConfirmationAuthority,
        store: ConsumptionStore,
        *,
        body_include: list[str] | None = None,
        query_include: list[str] | None = None,
        age_out_seconds: float = 300.0,
    ) -> None:
        self._authority = authority
        self._store = store
        self._body_include = body_include or []
        self._query_include = query_include
        self._age_out_seconds = age_out_seconds

    async def __call__(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        payload = getattr(request.state, "payment_payload", None)
        requirements = getattr(request.state, "payment_requirements", None)

        # Only gate nano:mainnet exact payments the SDK actually verified.
        requirements_dict = getattr(requirements, "model_dump", None)
        req = requirements.model_dump(by_alias=True) if callable(requirements_dict) else (requirements or {})
        if not (req.get("network") == NANO_NETWORK and req.get("scheme") == EXACT_SCHEME):
            return await call_next(request)
        if payload is None:
            return await call_next(request)

        send_hash = _send_hash_from_payload(payload)
        if not send_hash:
            return JSONResponse(
                content={"error": "payment missing a block hash; cannot bind consumption"},
                status_code=402,
            )

        check = self._authority.check(
            send_hash=send_hash,
            quoted_pay_to=req.get("payTo", ""),
            quoted_amount_raw=req.get("amount", ""),
        )
        if not check.confirmed:
            return self._unconfirmed_response(check)

        # Build the canonical request key (bind to the request, not a caller id).
        key = canonical_request_key(
            method=request.method,
            path=request.scope.get("root_path", "") + request.url.path,
            query=dict(request.query_params),
            query_include=self._query_include,
            body_include=self._body_include,
            body=getattr(request.state, "consumption_body", None),
        )

        decision = consume(
            self._store,
            send_hash=send_hash,
            request_key=key,
            age_out_seconds=self._age_out_seconds,
        )
        return await self._apply_decision(request, call_next, decision, send_hash)

    async def _apply_decision(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
        decision: "ConsumeDecision",
        send_hash: str,
    ) -> Response:
        if decision.kind == "reuse_rejected":
            return JSONResponse(
                content={"error": "payment already consumed by another request"},
                status_code=409,
            )
        if decision.kind == "outcome_unknown":
            # Never re-run the handler's side effects, never claim atomicity.
            return JSONResponse(
                content={
                    "error": "payment redemption outcome is unknown; do not retry side effects "
                    "against an assumed success"
                },
                status_code=409,
            )
        if decision.kind == "replay_completed":
            return JSONResponse(
                content={"result": decision.response_handle or "ok", "replayed": True},
                status_code=200,
            )

        # decision.kind == "proceed": run the handler, then record its outcome.
        try:
            response = await call_next(request)
        except Exception as error:  # handler outcome unknown
            self._store.resolve_unknown(send_hash)
            logger.exception("handler failed during a consumed payment; outcome marked unknown")
            raise error
        if response.status_code >= 400:
            # The handler returned an error; the payment is consumed but its
            # resource outcome was not produced. Record it so a retry does not
            # re-run the handler silently on a failed precedent.
            self._store.resolve_unknown(send_hash)
            return response
        try:
            self._store.complete(send_hash, str(response.status_code))
        except Exception:  # pragma: no cover - best effort on storage failure
            logger.exception("failed to record consumption outcome for %s", send_hash)
        return response

    def _unconfirmed_response(self, check: ConfirmationCheck) -> Response:
        return JSONResponse(
            content={"error": f"payment not confirmed: {check.reason}"},
            status_code=402,
        )


def nano_consumption_middleware(
    authority: ConfirmationAuthority,
    store: ConsumptionStore,
    *,
    body_include: list[str] | None = None,
    query_include: list[str] | None = None,
    age_out_seconds: float = 300.0,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    """Return a FastAPI middleware function (for use in ``@app.middleware``)."""
    instance = NanoConsumptionMiddleware(
        authority,
        store,
        body_include=body_include,
        query_include=query_include,
        age_out_seconds=age_out_seconds,
    )

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        return await instance(request, call_next)

    return middleware