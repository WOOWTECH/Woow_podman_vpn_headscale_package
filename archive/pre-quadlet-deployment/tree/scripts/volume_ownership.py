#!/usr/bin/env python3
"""Resolve only compose-owned volumes with exact project-scoped names."""

import argparse
import json
import subprocess


STACK_LABEL = "org.woow-headscale.project"
STACK_VALUE = "woow-headscale"
PROJECT_DIR_LABEL = "org.woow-headscale.project-dir"
PROJECT_LABELS = ("com.docker.compose.project", "io.podman.compose.project")


class OwnershipError(ValueError):
    pass


def _run(arguments, *, check=True):
    return subprocess.run(arguments, text=True, capture_output=True, check=check)


def _inspect(arguments, description):
    try:
        result = _run(arguments)
        values = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"cannot inspect {description}") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise OwnershipError(f"invalid inspection data for {description}")
    return values[0]


def _validate_ownership_labels(labels, project, project_dir, description):
    labels = labels or {}
    if labels.get(STACK_LABEL) != STACK_VALUE:
        raise OwnershipError(f"{description} is not owned by this stack")
    if any(labels.get(key) != project for key in PROJECT_LABELS):
        raise OwnershipError(f"{description} has mismatched compose project ownership labels")
    if labels.get(PROJECT_DIR_LABEL) != project_dir:
        raise OwnershipError(f"{description} belongs to another checkout")


def _container_exists(name):
    result = _run(["podman", "container", "exists", name], check=False)
    if result.returncode not in (0, 1):
        raise OwnershipError(f"cannot determine whether container {name} exists")
    return result.returncode == 0


def validate_container(name, project, project_dir):
    """Validate exact stack, Compose project, and checkout labels on a container."""
    if not _container_exists(name):
        return None
    value = _inspect(["podman", "inspect", name], f"container {name}")
    labels = value.get("Config", {}).get("Labels") or {}
    _validate_ownership_labels(labels, project, project_dir, f"container {name}")
    return value


def resolve_volume(container, destination, logical, project, project_dir, *, allow_missing=False):
    """Resolve an exact, labelled project volume and validate any container mount."""
    expected = f"{project}_{logical}"
    value = validate_container(container, project, project_dir)
    if value is not None:
        matches = [
            mount.get("Name")
            for mount in value.get("Mounts", [])
            if mount.get("Type") == "volume" and mount.get("Destination") == destination
        ]
        if matches != [expected]:
            raise OwnershipError(
                f"container {container} does not mount expected volume {expected} at {destination}"
            )

    listing = _run(["podman", "volume", "ls", "--format", "{{.Name}}"])
    names = set(listing.stdout.splitlines())
    if expected not in names:
        if allow_missing and value is None:
            return None
        raise OwnershipError(f"expected volume {expected} does not exist")

    volume = _inspect(["podman", "volume", "inspect", expected], f"volume {expected}")
    if volume.get("Name") != expected:
        raise OwnershipError(f"volume inspection did not return exact volume {expected}")
    _validate_ownership_labels(
        volume.get("Labels") or {}, project, project_dir, f"volume {expected}"
    )
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--project", required=True)
    resolve.add_argument("--project-dir", required=True)
    resolve.add_argument("--container", required=True)
    resolve.add_argument("--destination", required=True)
    resolve.add_argument("--logical", required=True)
    resolve.add_argument("--allow-missing", action="store_true")
    container = commands.add_parser("check-container")
    container.add_argument("--project", required=True)
    container.add_argument("--project-dir", required=True)
    container.add_argument("--container", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "resolve":
            value = resolve_volume(
                arguments.container,
                arguments.destination,
                arguments.logical,
                arguments.project,
                arguments.project_dir,
                allow_missing=arguments.allow_missing,
            )
            if value is not None:
                print(value)
        else:
            validate_container(arguments.container, arguments.project, arguments.project_dir)
    except (OwnershipError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
