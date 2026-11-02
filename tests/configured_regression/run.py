"""One offline command for the configured regression. SPDX-License-Identifier: MIT"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "x402": "2.26.0",
    "fastapi": "0.162.0",
    "starlette": "1.10.0",
    "httpx": "0.28.1",
    "uvicorn": "0.54.0",
    "pydantic": "2.14.2",
    "pytest": "8.3.4",
}


def main() -> int:
    if sys.version_info[:3] != (3, 12, 3):
        print(f"python pin mismatch: {sys.version.split()[0]}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy", "X402_NANO_LIVE"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    from importlib.metadata import version

    for name, want in EXPECTED.items():
        found = version(name)
        if found != want:
            print(f"{name} pin mismatch: {found} != {want}", file=sys.stderr)
            return 2
    os.environ["CONFIGURED_ATTEMPT_LOG"] = str(Path(__file__).resolve().parent / "attempts.jsonl")
    # Importing the tests installs the loopback guard before the app starts.
    import pytest

    target = Path(__file__).resolve().parent / "test_mounted.py"
    code = pytest.main(["-q", "--tb=line", "-p", "no:cacheprovider", str(target)])
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
