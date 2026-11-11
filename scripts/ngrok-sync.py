#!/usr/bin/env python3
"""Resolve the live ngrok URL before Headscale starts and sync installed configs.

The ngrok API is read from inside the agent container. No management port is
published. This file is installed under ~/.config/headscale by install.sh so
Quadlet can call it after a reboot without needing a Git checkout.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
API = "http://127.0.0.1:4040/api/tunnels"


def valid_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return (len(host) <= 253 and "." in host and
                all(DNS_LABEL.fullmatch(label) for label in host.split(".")))


def parse_public_url(payload: dict, mode: str, domain: str = "") -> str | None:
    """Select a single ngrok public endpoint and convert TCP to Headscale HTTP."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tunnels"), list):
        return None
    candidates: set[str] = set()
    for tunnel in payload["tunnels"]:
        if not isinstance(tunnel, dict) or not isinstance(tunnel.get("public_url"), str):
            continue
        raw = tunnel["public_url"]
        parsed = urlsplit(raw)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            continue
        host = (parsed.hostname or "").lower()
        if not valid_hostname(host) or parsed.netloc != parsed.netloc.lower():
            continue
        try:
            port = parsed.port
        except ValueError:
            continue
        if mode == "http" and parsed.scheme == "https" and port is None:
            url = f"https://{host}"
            if not domain or url == f"https://{domain.lower()}":
                candidates.add(url)
        if mode == "tcp" and parsed.scheme == "tcp" and port is not None:
            candidates.add(f"http://{host}:{port}")
    return next(iter(candidates)) if len(candidates) == 1 else None


def read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def discover(mode: str, domain: str, timeout: int) -> str | None:
    until = time.monotonic() + timeout
    while True:
        try:
            response = subprocess.run(
                ["podman", "exec", "headscale-ngrok", "wget", "-qO-", API],
                capture_output=True, text=True, timeout=5, check=True
            )
            url = parse_public_url(json.loads(response.stdout), mode, domain)
            if url:
                return url
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        if time.monotonic() >= until:
            return None
        time.sleep(2)


def atomic_replace(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.ngrok-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_url(config_dir: Path, url: str) -> bool:
    """Update the two rendered configs and their owner env; no credential is touched."""
    env_file = config_dir / "headscale.env"
    env = read_env(env_file)
    base = env.get("HEADSCALE_BASE_DOMAIN", "ts.local").lower()
    host = urlsplit(url).hostname or ""
    if host == base or host.endswith("." + base):
        raise ValueError("ngrok hostname overlaps the MagicDNS base domain")
    # Check every target before writing anything, then atomically replace each.
    targets = (
        (env_file, r"^HEADSCALE_SERVER_URL=.*$", "HEADSCALE_SERVER_URL=" + url),
        (config_dir / "headscale/config.yaml", r"^server_url: .*$", "server_url: " + url),
        (config_dir / "headplane/config.yaml", r'^  public_url: ".*"$',
         '  public_url: "' + url + '"'),
    )
    updates: list[tuple[Path, str, str]] = []
    for path, pattern, value in targets:
        before = path.read_text()
        matches = list(re.finditer(pattern, before, flags=re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"expected setting missing or duplicated in {path.name}")
        after = before[:matches[0].start()] + value + before[matches[0].end():]
        if before != after:
            updates.append((path, before, after))
    written: list[tuple[Path, str]] = []
    try:
        for path, before, after in updates:
            atomic_replace(path, after)
            written.append((path, before))
    except OSError:
        for path, before in reversed(written):
            atomic_replace(path, before)
        raise
    return bool(updates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--print-url", action="store_true",
                        help="print only the matching live URL; fail when none is online")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/headscale")
    parser.add_argument("--timeout", type=int, default=90)
    options = parser.parse_args()
    env = read_env(options.config_dir / "headscale.env")
    if env.get("HEADSCALE_NGROK_ENABLED", "false") != "true":
        return 0
    mode = env.get("HEADSCALE_NGROK_MODE", "http")
    domain = env.get("HEADSCALE_NGROK_DOMAIN", "") if mode == "http" else ""
    if mode not in {"http", "tcp"} or (domain and not valid_hostname(domain.lower())):
        print("ngrok-sync: invalid mode or domain", file=sys.stderr)
        return 1
    if options.timeout < 0 or options.timeout > 300:
        print("ngrok-sync: timeout must be 0..300", file=sys.stderr)
        return 1
    url = discover(mode, domain, options.timeout)
    if not url:
        print("ngrok-sync: no matching live tunnel; keeping the last URL", file=sys.stderr)
        return 1 if options.print_url else 0  # keep the local server available
    if options.print_url:
        print(url)
        return 0
    try:
        changed = apply_url(options.config_dir, url) if options.apply else False
    except (OSError, ValueError) as error:
        print(f"ngrok-sync: {error}", file=sys.stderr)
        return 1
    print(f"ngrok-sync: {url}" + (" (updated)" if changed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
