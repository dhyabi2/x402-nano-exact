"""x402 ``exact`` scheme on ``nano:mainnet`` for the x402 Python SDK (resource-server side).

Usage::

    from x402 import x402ResourceServer
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient
    from x402_nano_exact import ExactNanoServerScheme

    nano_facilitator = HTTPFacilitatorClient(FacilitatorConfig(url="https://facilitator.pursekeeper.dev"))
    server = x402ResourceServer([nano_facilitator])
    server.register("nano:mainnet", ExactNanoServerScheme())
"""

from .address import is_nano_address, normalize_nano_address
from .confirmation import (
    BlockFacts,
    ConfirmationAuthority,
    ConfirmationCheck,
    NanoNode,
    normalize_pay_to,
    require_hex_64,
)
from .constants import (
    ASSET_XNO,
    NETWORK_NANO_MAINNET,
    RAW_PER_XNO,
    SCHEME_EXACT,
    SEND_BLOCK_WORK_THRESHOLD,
)
from .consumption import (
    ConcurrentBind,
    ConsumptionRow,
    ConsumptionStore,
    ConsumeDecision,
    canonical_request_key,
    consume,
)
from .server import ExactNanoScheme, xno_to_raw
from .rpc_node import HttpNanoNode, NanoRpcError
from .sqlite_store import SqliteConsumptionStore

ExactNanoServerScheme = ExactNanoScheme
"""SDK-style alias (cf. ``x402.mechanisms.tvm.exact.ExactTvmServerScheme``)."""

__all__ = [
    "ASSET_XNO",
    "NETWORK_NANO_MAINNET",
    "RAW_PER_XNO",
    "SCHEME_EXACT",
    "SEND_BLOCK_WORK_THRESHOLD",
    "ConcurrentBind",
    "ConfirmationAuthority",
    "ConfirmationCheck",
    "ConsumeDecision",
    "ConsumptionRow",
    "ConsumptionStore",
    "BlockFacts",
    "ExactNanoScheme",
    "ExactNanoServerScheme",
    "HttpNanoNode",
    "NanoNode",
    "NanoRpcError",
    "SqliteConsumptionStore",
    "canonical_request_key",
    "consume",
    "is_nano_address",
    "normalize_nano_address",
    "normalize_pay_to",
    "require_hex_64",
    "xno_to_raw",
]
