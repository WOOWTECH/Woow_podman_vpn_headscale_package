#!/usr/bin/env python3
"""Check the stored Headplane API key against Headscale's own key list.

The key itself arrives on stdin, so it never appears in argv, in `ps`, or in a shell history.
The list JSON is a file path: `headscale apikeys list` prints masked prefixes only, no key
material. Exit 0 when the key is a live, unexpired key of this Headscale; 1 otherwise, with
the reason on stderr. The reason never quotes the key.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone

API_KEY = re.compile(r"hskey-api-([A-Za-z0-9_-]{12})-([A-Za-z0-9_-]{64})", re.ASCII)
MARKER = "hskey-api-"
INVALID_STATUS = {"disabled", "expired", "invalid", "revoked"}


def records(payload):
    """Headscale has spelled this list three ways across releases; accept all of them."""
    if isinstance(payload, dict):
        for name in ("api_keys", "apikeys", "items"):
            if name in payload:
                payload = payload[name]
                break
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        raise ValueError("headscale returned an API key list that is not a list")
    return payload


def expiration(value):
    """Either an RFC3339 string or a protobuf {seconds, nanos} timestamp."""
    if isinstance(value, dict):
        seconds, nanos = value.get("seconds"), value.get("nanos", 0)
        if isinstance(seconds, bool) or isinstance(nanos, bool):
            raise ValueError("the API key record has no usable expiration")
        try:
            return datetime.fromtimestamp(int(seconds) + int(nanos) / 1e9, timezone.utc)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("the API key record has no usable expiration") from exc
    if not isinstance(value, str) or not value:
        raise ValueError("the API key record has no usable expiration")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("the API key record has an unreadable expiration") from exc
    if parsed.tzinfo is None:
        raise ValueError("the API key record has an expiration without a time zone")
    return parsed.astimezone(timezone.utc)


def visible_prefix(value):
    """The list masks a key as "<prefix>***"; return the part that is actually shown."""
    if not isinstance(value, str) or not value.endswith("***"):
        return None
    shown = value[:-3]
    if not shown.startswith(MARKER) or len(shown) < len(MARKER) + 12:
        return None
    return shown


def validate(key_text, payload, now=None):
    key = key_text.strip()
    if not API_KEY.fullmatch(key):
        raise ValueError("the stored Headplane API key is not shaped like a headscale API key")
    matches = [
        item
        for item in records(payload)
        if isinstance(item, dict)
        and (shown := visible_prefix(item.get("prefix"))) is not None
        and key.startswith(shown)
    ]
    if len(matches) != 1:
        raise ValueError("the stored Headplane API key matches no single key of this headscale")
    record = matches[0]
    for field in ("revoked", "expired", "disabled", "invalid"):
        if record.get(field) is True:
            raise ValueError(f"the stored Headplane API key is {field}")
    status = record.get("status")
    if record.get("valid") is False or (isinstance(status, str) and status.lower() in INVALID_STATUS):
        raise ValueError("headscale reports the stored Headplane API key as not valid")
    if expiration(record.get("expiration")) <= (now or datetime.now(timezone.utc)):
        raise ValueError("the stored Headplane API key has expired")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-json", required=True, help="output of `headscale apikeys list --output json`")
    arguments = parser.parse_args()
    try:
        with open(arguments.list_json, encoding="utf-8") as source:
            validate(sys.stdin.read(), json.load(source))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"validate-api-key: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
