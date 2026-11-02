# x402-nano-exact

Resource-server side of the x402 v2 **`exact`** scheme on **`nano:mainnet`** for the
official [x402 Python SDK](https://pypi.org/project/x402/) (`x402 >= 2.22`).
It is the Python counterpart of the JS package
[`@x402nano/exact`](https://github.com/x402nano/exact) (`./server` export): it
parses prices into raw and builds the `accepts` entry; verification and
settlement are delegated by the SDK to a Nano facilitator such as
[facilitator.pursekeeper.dev](https://facilitator.pursekeeper.dev).

Pure Python; the only dependency is `x402` itself (`x402[httpx]` for the HTTP
facilitator client).

## Quick start

Nano only. Needs the HTTP facilitator client, which is an extra: `pip install 'x402-nano-exact[http]'` (it adds `x402[httpx]`; a bare `x402` install fails at `initialize()` with `No module named 'httpx'`; verified 2026-09-12 with pip 25 into a clean target). Published on [PyPI](https://pypi.org/project/x402-nano-exact/) since 2026-09-18 under the pursekeeper account; the name was offered to the x402nano organisation first (x402nano/exact#4, unanswered) and the offer to transfer the project stands. Development install: `pip install 'x402-nano-exact[http] @ git+https://github.com/pursekeeper/x402-nano-exact'`.

```python
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402_nano_exact import ExactNanoServerScheme

nano_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url="https://facilitator.pursekeeper.dev", timeout=45.0))

server = x402ResourceServer([nano_facilitator])
server.register("nano:mainnet", ExactNanoServerScheme())
server.initialize()  # fetches /supported and routes nano:mainnet to the Nano facilitator
```

Adding Nano next to an existing Base USDC rail needs two more things: the SDK's EVM extra
(`pip install 'x402[evm,httpx]'`, otherwise the import below raises `ImportError: EVM mechanism
requires ethereum packages`) and a facilitator whose `/supported` lists the Base network you
register. `https://x402.org/facilitator` serves Base Sepolia (`eip155:84532`) only; for Base
mainnet (`eip155:8453`) use the facilitator your USDC rail already uses.

```python
from x402.mechanisms.evm.exact import ExactEvmServerScheme  # needs x402[evm]

base_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url="https://<the facilitator your Base rail uses>"))
nano_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url="https://facilitator.pursekeeper.dev", timeout=45.0))

server = x402ResourceServer([base_facilitator, nano_facilitator])
server.register("eip155:8453", ExactEvmServerScheme())
server.register("nano:mainnet", ExactNanoServerScheme())
server.initialize()  # routes each network to the facilitator that advertises it
```

Then use the server exactly as for any other network. A route accepting both:

```python
routes = {
    "GET /api/weather": {
        "accepts": [
            {"scheme": "exact", "network": "eip155:8453", "price": "$0.01", "payTo": "0x..."},
            {"scheme": "exact", "network": "nano:mainnet", "price": "0.01", "payTo": "nano_...", "maxTimeoutSeconds": 60},
        ]
    }
}
```

See `examples/fastapi_server.py` for the full FastAPI middleware version (dual rail, so it needs
`pip install 'x402[evm,httpx,fastapi]'`).

## What goes on the wire

`price: "0.01"` on `nano:mainnet` produces this `accepts` entry (1 XNO = 10^30 raw):

```json
{"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
 "amount": "10000000000000000000000000000", "payTo": "nano_...", "maxTimeoutSeconds": 60,
 "extra": {"work": "required", "workThreshold": "fffffff800000000"}}
```

`extra.work` / `extra.workThreshold` are copied from the facilitator's `/supported`
entry; values you set in `extra` yourself take precedence.

## Prices

| form | meaning |
| --- | --- |
| `"0.01"`, `0.01`, `"0.01 XNO"` | XNO, converted exactly to raw |
| `{"amount": "10000000000000000000000000000", "asset": "XNO"}` / `AssetAmount(...)` | already raw |
| `"$0.01"`, `"0.01 USD"` (case-insensitive USD) | rejected unless you `register_money_parser(...)` a fiat converter |

Rejected: zero/negative, more than 30 decimal places, other assets, and integer
amounts above the Nano supply (a raw amount typed as XNO — pass it as `AssetAmount`).
Note the JS `@x402nano/exact` reads integer strings as raw; here Money is always XNO,
matching the SDK's convention for `Money`.

`payTo` is checksum-validated at requirement build time; `xrb_` is normalized to `nano_`.

## Notes

- Payment flow is `authorization` only: verify before the handler, settle after it
  (the buyer is never charged for a failed request). Same as the JS package.
- The facilitator's `/settle` waits for block confirmation for up to 30 s; set
  `FacilitatorConfig(timeout=45.0)` so the SDK's HTTP client does not give up first.
- `maxTimeoutSeconds` defaults to the SDK's 300 if you do not set it; 60 is customary.

## Pitfalls seen in the first integrations

Reported by the first two sellers who wired this in (2026-09-11), with credit. Both are in production now: api.shehriyar.ink (OreoMuncher45, 38 endpoints, USDC on Base plus XNO) and nano-csv-service.onrender.com (Reeyen Patel).

- **Import the right module.** The package is `x402_nano_exact`, not `x402nano`. One seller wrapped the
  registration in a broad `except` and a wrong module name turned into "Nano rail silently disabled on
  every boot", which looks identical to "not enabled". Let the import fail loudly, or log the exception.
  (OreoMuncher45)
- **Module-level names are evaluated before your feature flag.** A `NANO_ADDRESS` default referenced at
  import time, before the `NANO_ENABLED` check, raised `NameError` on startup and would have taken a
  40-endpoint API down with it. Declare the payout address at module scope or read it inside the flag.
  (OreoMuncher45)
- **Two networks, two facilitators, both must answer `/supported`.** Running without the Base facilitator
  key silently fell back to the x402.org testnet facilitator, which does not serve `eip155:8453`; every
  EVM route then failed `RouteConfigurationError` and nothing about it pointed at Nano. Check each
  facilitator's `/supported` for its own network before blaming the new scheme. (OreoMuncher45)
- **Price is integer XNO, not raw and not `"$0.01"`.** `Money` follows the SDK convention; a string
  dollar price is rejected on purpose rather than converted at a guessed rate. (Kept after review by
  OreoMuncher45; open an issue if you disagree.)
- **Install it somewhere that survives `/tmp`, and not editable.** One production instance had the
  package installed editable from `/tmp/pk-nano` (`direct_url.json` said so). Nothing fails until a tmp
  cleanup or reboot removes the source; then the import raises, the broad `except` around registration
  turns that into `NANO_ENABLED = False`, and the rail disappears with one log line. Install from a
  durable path, non-editable, and check `pip show -f` once. (OreoMuncher45)
- **Price display is per rail.** If you post-process `accepts` by looking the price up from the request
  path, a second rail inherits the first rail's fiat string: the Nano entry came back with
  `"price": "$0.003"` next to a correct raw `amount`. Machines paying by `amount` were fine; anyone
  reading `price` was not. Branch on `network == "nano:mainnet"` when enriching. (OreoMuncher45)
- **Free hosting sleeps.** A seller on Render Free takes about a minute to wake; fetch a health route
  with a long timeout first, then get a fresh 402 before building the block, since `maxTimeoutSeconds`
  starts at the quote. (Reeyen Patel, github.com/Reeyenn/nano-csv-service, the first live seller on
  this scheme; settled through facilitator.pursekeeper.dev on 2026-09-11.)
- **The EVM scheme is an extra, and x402.org's facilitator is testnet.** `from x402.mechanisms.evm.exact
  import ExactEvmServerScheme` raises `ImportError` unless `x402[evm]` is installed, and
  `https://x402.org/facilitator` advertises `eip155:84532` (Base Sepolia), not `eip155:8453`. An
  earlier quick start here paired them; it is now Nano-only first, dual rail second. (jackspiece,
  github.com/jackspiece, 2026-09-11, reproduced in a fresh venv.)

## Tests

```
pip install -e ".[dev]"
pytest                    # offline: SDK-built 402 shape and verify path with a fake facilitator
X402_NANO_LIVE=1 pytest   # plus one live /supported + malformed /verify against facilitator.pursekeeper.dev
```

## Consumption-binding middleware (confirm + single-use)

The SDK's `payment_middleware` verifies a payment and settles it, but it does
**not** (by itself) guarantee that the confirmed send is redeemed by *exactly
one* protected request, nor that a retry of the same request recovers its
stored outcome instead of re-running the handler. `x402-nano-exact` ships a
small, source-only middleware that adds those two guarantees on the
resource-server side:

```python
from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402_nano_exact import ExactNanoServerScheme
from x402_nano_exact.confirmation import ConfirmationAuthority
from x402_nano_exact.middleware import nano_consumption_middleware
from x402_nano_exact.rpc_node import HttpNanoNode
from x402_nano_exact.sqlite_store import SqliteConsumptionStore

server = x402ResourceServer([HTTPFacilitatorClient(FacilitatorConfig(url="https://facilitator.pursekeeper.dev"))])
server.register("nano:mainnet", ExactNanoServerScheme())
server.initialize()
routes = {"GET /api/weather": {"accepts": [{"scheme": "exact", "network": "nano:mainnet", "payTo": "nano_...", "price": "0.01"}]}}

app = FastAPI()
# Register the guard FIRST; Starlette runs the LAST-added middleware first, so
# x402's verify ends up outermost and this guard runs in the verified-but-
# before-handler slot. Registered the other way round, the guard would run
# before the SDK verified anything and pass all requests through un-gated.
app.middleware("http")(
    nano_consumption_middleware(
        ConfirmationAuthority(HttpNanoNode()),           # Nano RPC = the confirmation authority
        SqliteConsumptionStore("consumption.db"),        # durable single-use ledger
        body_include=["prompt"],                          # bind to these body/query inputs too
    )
)
# Outer (registered last -> runs first): x402's verify -> handler -> settle flow.
app.middleware("http")(payment_middleware(routes, server))
```

What it enforces for every `nano:mainnet` / `exact` verified payment:

* **Confirmation of the exact quoted send hash.** The payment is served only
  after the quoted send block is *cemented* by the node you point at (default
  `https://rpc.nano.to`), and only when that block's recipient and amount equal
  the quoted `payTo`/`amount`. A `process-accept` hash, a newer account
  frontier, or a cemented block that pays a different payee/amount is not
  confirmation.
* **Single-use, bound to the request.** The confirmed send is keyed in the
  durable store to the canonical request (method + path + chosen query/body
  inputs — never a caller-chosen request id alone). Presenting the same
  payment against a *different* request is rejected (409); a retry of the
  *same* request recovers the stored outcome and never runs the handler twice.
* **Crash-safe outcomes.** If the process dies between consuming the payment
  and recording the handler result, the row is retained as `outcome_unknown`
  (after an age-out) rather than re-running the handler's side effects or
  claiming an atomic HTTP guarantee.

It never signs, publishes or funds anything, and it leaves the SDK's own
`verify`/`settle` in place — it only adds the two guarantees the stack does
not currently provide.
