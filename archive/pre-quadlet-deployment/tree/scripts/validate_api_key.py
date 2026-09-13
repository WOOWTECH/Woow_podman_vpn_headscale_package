#!/usr/bin/env python3
"""Validate a persisted Headscale API key against Headscale's key-list JSON."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys


API_KEY = re.compile(r"hskey-api-([A-Za-z0-9_-]{12})-([A-Za-z0-9_-]{64})", re.ASCII)
API_KEY_MARKER = "hskey-api-"
MIN_VISIBLE_KEY_CHARACTERS = 12
INVALID_STATUSES = {"disabled", "expired", "invalid", "revoked"}


def _records(payload):
    if isinstance(payload, dict):
        for name in ("api_keys", "apikeys", "items"):
            if name in payload:
                payload = payload[name]
                break
    if not isinstance(payload, list):
        raise ValueError("Headscale returned an invalid API key list")
    return payload


def _expiration(value):
    error = "the stored Headplane API key has no valid expiration"
    if isinstance(value, dict):
        seconds = value.get("seconds")
        nanos = value.get("nanos", 0)
        try:
            if isinstance(seconds, bool) or isinstance(nanos, bool):
                raise ValueError
            seconds = int(seconds)
            nanos = int(nanos)
            original_seconds = value.get("seconds")
            original_nanos = value.get("nanos", 0)
            if str(seconds) != str(original_seconds) or str(nanos) != str(original_nanos):
                raise ValueError
            if not 0 <= nanos < 1_000_000_000:
                raise ValueError
            return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, timezone.utc)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(error) from exc
    if not isinstance(value, str) or not value:
        raise ValueError(error)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(error) from exc
    if parsed.tzinfo is None:
        raise ValueError(error)
    return parsed.astimezone(timezone.utc)


def _visible_prefix(value):
    if not isinstance(value, str) or not value.endswith("***"):
        return None
    visible = value[:-3]
    if not visible.startswith(API_KEY_MARKER):
        return None
    if len(visible) < len(API_KEY_MARKER) + MIN_VISIBLE_KEY_CHARACTERS:
        return None
    return visible


def validate_api_key(key_text, payload, now=None):
    """Require one masked list prefix to identify the full persisted key."""
    key = key_text.strip()
    match = API_KEY.fullmatch(key)
    if not match:
        raise ValueError("the stored Headplane API key has an invalid format")
    prefix = match.group(1)
    matches = [
        item
        for item in _records(payload)
        if isinstance(item, dict)
        and (visible := _visible_prefix(item.get("prefix"))) is not None
        and key.startswith(visible)
    ]
    if len(matches) != 1:
        raise ValueError("the stored Headplane API key has no unique Headscale key record")

    record = matches[0]
    for field in ("revoked", "expired", "disabled", "invalid"):
        if record.get(field) is True:
            raise ValueError("the stored Headplane API key is not valid")
    status = record.get("status")
    if record.get("valid") is False or (
        isinstance(status, str) and status.lower() in INVALID_STATUSES
    ):
        raise ValueError("the stored Headplane API key is not valid")
    current_time = now or datetime.now(timezone.utc)
    if _expiration(record.get("expiration")) <= current_time:
        raise ValueError("the stored Headplane API key is expired")
    return prefix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--list-json", required=True)
    arguments = parser.parse_args(argv)
    try:
        key_path = Path(arguments.key_file)
        if key_path.is_symlink():
            raise ValueError("the stored Headplane API key must not be a symbolic link")
        key = key_path.read_text(encoding="utf-8")
        with Path(arguments.list_json).open(encoding="utf-8") as source:
            payload = json.load(source)
        validate_api_key(key, payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
