# Configured x402 regression

Apply this directory on `dhyabi2/x402-nano-exact` commit
`f0b3e89463d4b291cab95830151c4c1c05ff04b6`.

One command, from the repository root, with network only for the install:

```
python3 -m pip install -r tests/configured_regression/requirements.txt && python3 tests/configured_regression/run.py
```

`run.py` requires CPython 3.12.3 and the versions in `requirements.txt`.
It mounts `payment_middleware` and `nano_consumption_middleware` in the
documented order, on loopback, and refuses any other socket. `X402_NANO_LIVE`
is unset. A same-request retry must skip the handler and is still settled
again by the SDK. That settlement attempt is the current boundary.
