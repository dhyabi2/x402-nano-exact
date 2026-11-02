"""FastAPI seller accepting XNO on nano:mainnet with confirmation + single-use
consumption binding.

Composes this package's ``nano_consumption_middleware`` around the x402 SDK's
``payment_middleware`` (registered after it, so it runs in the verified-but-
before-handler slot). Every ``nano:mainnet``/``exact`` payment is served only
once its quoted send block is cemented by the Nano RPC node (default
https://rpc.nano.to) and bound to exactly one protected request.

Run:  pip install "x402[fastapi,httpx]" x402-nano-exact
      uvicorn fastapi_consumption:app --port 4021
"""

from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware

from x402_nano_exact import ExactNanoServerScheme
from x402_nano_exact.confirmation import ConfirmationAuthority
from x402_nano_exact.middleware import nano_consumption_middleware
from x402_nano_exact.rpc_node import HttpNanoNode
from x402_nano_exact.sqlite_store import SqliteConsumptionStore

server = x402ResourceServer(
    [
        HTTPFacilitatorClient(
            # /settle waits for confirmation up to 30 s; give HTTP more time.
            FacilitatorConfig(url="https://facilitator.pursekeeper.dev", timeout=45.0)
        )
    ]
)
server.register("nano:mainnet", ExactNanoServerScheme())
server.initialize()

routes = {
    "POST /api/chat": {
        "accepts": [
            {
                "scheme": "exact",
                "network": "nano:mainnet",
                "price": "0.01",  # XNO
                "payTo": "nano_3mzq6kpu4b56w1afgouwumfc8juriqe3w1t14xf1f3tgc8fn4puepfrjhcfk",
                "maxTimeoutSeconds": 60,
            },
        ],
        "description": "A paid chat completion",
        "mimeType": "application/json",
    }
}

app = FastAPI()
# Outer: x402's own verify -> handler -> settle flow (takes a real payment).
app.middleware("http")(payment_middleware(routes, server))
# Inner: confirm cementation + bind the payment to exactly one request.
app.middleware("http")(
    nano_consumption_middleware(
        ConfirmationAuthority(HttpNanoNode()),       # the confirmation authority
        SqliteConsumptionStore("consumption.db"),    # durable single-use ledger
        body_include=["prompt"],                     # bind to these body inputs too
        age_out_seconds=300.0,
    )
)


@app.post("/api/chat")
def chat(body: dict):
    return {"reply": f"hi, you paid for: {body.get('prompt', '')}"}