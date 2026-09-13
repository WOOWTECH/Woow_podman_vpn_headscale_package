#!/usr/bin/env python3
"""Unit tests for scripts/validate-api-key.py. No podman, no network, no key material:
every key below is a syntactically valid but entirely made-up string."""
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

spec = importlib.util.spec_from_file_location(
    "validator", pathlib.Path(__file__).resolve().parent.parent / "scripts/validate-api-key.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

KEY = "hskey-api-" + "a" * 12 + "-" + "b" * 64
OTHER = "hskey-api-" + "c" * 12 + "-" + "d" * 64
LATER = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
EARLIER = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def listing(prefix=KEY[:22], expiration=LATER, **extra):
    return [dict({"prefix": prefix + "***", "expiration": expiration}, **extra)]


def fails(key, payload, why):
    try:
        validator.validate(key, payload)
    except ValueError:
        return
    raise AssertionError(f"expected a rejection: {why}")


def main():
    validator.validate(KEY, listing())
    validator.validate(KEY + "\n", listing())
    validator.validate(KEY, {"api_keys": listing()})
    validator.validate(KEY, listing(expiration={"seconds": int(datetime.now(timezone.utc).timestamp()) + 86400, "nanos": 0}))
    fails("not-a-key", listing(), "malformed key")
    fails(KEY, [], "no matching record")
    fails(KEY, None, "empty listing")
    fails(OTHER, listing(), "the key belongs to another headscale")
    fails(KEY, listing(expiration=EARLIER), "expired")
    fails(KEY, listing(revoked=True), "revoked")
    fails(KEY, listing(status="disabled"), "disabled")
    fails(KEY, listing(valid=False), "reported invalid")
    fails(KEY, [{"prefix": KEY[:22], "expiration": LATER}], "prefix without the *** mask")
    fails(KEY, listing(expiration=None), "no expiration")
    fails(KEY, {"api_keys": "nonsense"}, "listing is not a list")
    print("validate-api-key: 15 case(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
