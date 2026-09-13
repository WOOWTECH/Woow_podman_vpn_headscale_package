#!/usr/bin/env python3
"""Validate that existing host listeners belong to the expected Podman container."""

import argparse
import ipaddress
import json
from pathlib import Path
import subprocess


def _normalize_address(value):
    value = str(value or "0.0.0.0").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if "%" in value:
        value = value.split("%", 1)[0]
    if value == "*":
        return value
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def parse_listener_addresses(output, port):
    """Return local addresses listening on port from ``ss -H -ltn`` output."""
    addresses = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        endpoint = fields[3]
        try:
            address, endpoint_port = endpoint.rsplit(":", 1)
        except ValueError:
            continue
        if endpoint_port != str(port):
            continue
        addresses.append(_normalize_address(address))
    return addresses


def addresses_overlap(requested, listening):
    """Conservatively determine whether two host addresses can share a socket."""
    requested = _normalize_address(requested)
    listening = _normalize_address(listening)
    if "*" in {requested, listening}:
        return True
    if listening == "::":
        return True  # The kernel may expose a dual-stack wildcard socket.
    if requested == "0.0.0.0":
        try:
            return ipaddress.ip_address(listening).version == 4
        except ValueError:
            return False
    if listening == "0.0.0.0":
        try:
            return ipaddress.ip_address(requested).version == 4
        except ValueError:
            return False
    return requested == listening


def _binding_covers_listener(binding_address, listener_address):
    binding_address = _normalize_address(binding_address)
    listener_address = _normalize_address(listener_address)
    if listener_address == "*":
        return binding_address in {"0.0.0.0", "::", "*"}
    return binding_address == listener_address


def inspect_is_project_container(item, project, project_dir):
    labels = item.get("Config", {}).get("Labels") or {}
    return (
        labels.get("org.woow-headscale.project") == "woow-headscale"
        and labels.get("org.woow-headscale.project-dir") == str(Path(project_dir).resolve())
        and labels.get("com.docker.compose.project") == project
        and labels.get("io.podman.compose.project") == project
    )


def owner_covers_listeners(
    item, requested_address, port, listener_addresses, project, project_dir
):
    """Return true only when every overlapping listener is an expected binding."""
    relevant = [
        address
        for address in listener_addresses
        if addresses_overlap(requested_address, address)
    ]
    if not relevant:
        return True
    if item.get("State", {}).get("Running") is not True:
        return False
    if not inspect_is_project_container(item, project, project_dir):
        return False
    ports = item.get("NetworkSettings", {}).get("Ports", {}) or {}
    bindings = [
        binding
        for values in ports.values()
        if values
        for binding in values
        if str(binding.get("HostPort", "")) == str(port)
    ]
    return all(
        any(
            _binding_covers_listener(binding.get("HostIp", ""), listener)
            for binding in bindings
        )
        for listener in relevant
    )


def check_port(requested_address, port, owner, project, project_dir):
    listeners_result = subprocess.run(
        ["ss", "-H", "-ltn", f"sport = :{port}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if listeners_result.returncode != 0:
        return False
    listeners = parse_listener_addresses(listeners_result.stdout, port)
    if not any(addresses_overlap(requested_address, address) for address in listeners):
        return True
    inspect_result = subprocess.run(
        ["podman", "inspect", owner], text=True, capture_output=True, check=False
    )
    if inspect_result.returncode != 0:
        return False
    try:
        items = json.loads(inspect_result.stdout)
        item = items[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return False
    return owner_covers_listeners(
        item, requested_address, port, listeners, project, project_dir
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check")
    check.add_argument("--address", required=True)
    check.add_argument("--port", required=True, type=int)
    check.add_argument("--owner", required=True)
    check.add_argument("--project", required=True)
    check.add_argument("--project-dir", required=True)
    overlap = subcommands.add_parser("overlap")
    overlap.add_argument("address")
    overlap.add_argument("port", type=int)
    overlap.add_argument("other_address")
    overlap.add_argument("other_port", type=int)
    arguments = parser.parse_args(argv)
    if arguments.command == "overlap":
        conflicts = arguments.port == arguments.other_port and addresses_overlap(
            arguments.address, arguments.other_address
        )
        return 0 if conflicts else 1
    return 0 if check_port(
        arguments.address, arguments.port, arguments.owner,
        arguments.project, arguments.project_dir
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
