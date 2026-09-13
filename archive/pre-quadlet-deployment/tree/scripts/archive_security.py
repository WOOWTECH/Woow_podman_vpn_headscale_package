#!/usr/bin/env python3
"""Safety checks and extraction for Woow Headscale backup archives."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile

FORMAT = "woow-headscale-backup-v1"
REQUIRED = {
    "manifest.json",
    "project/.env",
    "project/config/headscale/policy.json",
    "project/runtime/headscale/config.yaml",
    "project/runtime/headscale/extra_records.json",
    "project/runtime/headplane/config.yaml",
    "project/runtime/headplane/cookie-secret",
    "project/runtime/headplane/api-key",
    "volumes/headscale-data.tar",
    "volumes/headplane-data.tar",
}
MAX_MEMBERS = 100_000
MAX_TOTAL_SIZE = 20 * 1024 * 1024 * 1024


class UnsafeArchive(ValueError):
    pass


def _normalized_name(name):
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise UnsafeArchive("archive contains an unsafe member path")
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        return "."
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeArchive("archive contains an unsafe member path")
    return str(path)


def _validate_members(archive, *, nested=False):
    total = 0
    names = set()
    members = archive.getmembers()
    if len(members) > MAX_MEMBERS:
        raise UnsafeArchive("archive contains too many members")
    for member in members:
        name = _normalized_name(member.name.rstrip("/"))
        if name == "." and not (nested and member.isdir()):
            raise UnsafeArchive("archive contains an unsafe member path")
        if name in names:
            raise UnsafeArchive("archive contains duplicate members")
        names.add(name)
        if not (member.isfile() or member.isdir()):
            raise UnsafeArchive("archive contains links or special files")
        if member.size < 0:
            raise UnsafeArchive("archive contains an invalid member size")
        total += member.size
        if total > MAX_TOTAL_SIZE:
            raise UnsafeArchive("archive expands beyond the size limit")
    if nested and not names:
        raise UnsafeArchive("volume archive is empty")
    return names


def _contains_symlink_component(path):
    current = Path(path).absolute()
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current == current.parent:
            return False
        current = current.parent


@contextmanager
def _open_trusted_archive(path):
    """Open and authenticate an archive without following the final component."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise UnsafeArchive("archive is not readable or is a symbolic link") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeArchive("archive must be a regular file")
        if info.st_uid != os.getuid():
            raise UnsafeArchive("archive must be owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise UnsafeArchive("archive must not be writable by group or others")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            with tarfile.open(fileobj=source, mode="r:*") as archive:
                yield archive
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, UnsafeArchive):
            raise
        raise UnsafeArchive("archive is not a valid tar archive") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_outer_archive(archive):
    names = _validate_members(archive)
    if not REQUIRED.issubset(names):
        raise UnsafeArchive("archive is missing required backup members")
    manifest_member = archive.getmember("manifest.json")
    if manifest_member.size > 16_384:
        raise UnsafeArchive("backup manifest is too large")
    source = archive.extractfile(manifest_member)
    if source is None:
        raise UnsafeArchive("backup manifest is unreadable")
    try:
        manifest = json.load(source)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UnsafeArchive("backup manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise UnsafeArchive("unsupported backup format")
    for nested_name in ("volumes/headscale-data.tar", "volumes/headplane-data.tar"):
        member = archive.getmember(nested_name)
        nested_source = archive.extractfile(member)
        if nested_source is None:
            raise UnsafeArchive("volume archive is unreadable")
        # Spool instead of trusting seek support from a compressed outer stream.
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as payload:
            shutil.copyfileobj(nested_source, payload)
            payload.seek(0)
            try:
                with tarfile.open(fileobj=payload, mode="r:*") as nested:
                    _validate_members(nested, nested=True)
            except tarfile.TarError as exc:
                raise UnsafeArchive("volume archive is invalid") from exc
    return manifest


def validate_archive(path):
    """Validate the outer archive, its manifest, and both nested volume archives."""
    with _open_trusted_archive(path) as archive:
        return _validate_outer_archive(archive)


def extract_archive(path, destination):
    """Validate and extract from one authenticated archive descriptor."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise UnsafeArchive("extraction destination must not exist")
    try:
        with _open_trusted_archive(path) as archive:
            _validate_outer_archive(archive)
            destination.mkdir(mode=0o700)
            for member in archive.getmembers():
                normalized = _normalized_name(member.name.rstrip("/"))
                relative = PurePosixPath(normalized)
                target = destination.joinpath(*relative.parts)
                try:
                    target.relative_to(destination)
                except ValueError as exc:
                    raise UnsafeArchive("archive member escapes extraction destination") from exc
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(target, 0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise UnsafeArchive("archive member is unreadable")
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(target, 0o600)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def safe_output_path(value, filename):
    """Resolve a new archive path below a trusted, non-symlink directory."""
    value = os.fspath(value)
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("unsafe output path")
    requested = Path(value).expanduser()
    if requested.exists() and requested.is_dir():
        requested = requested / filename
    if requested.exists() or requested.is_symlink():
        raise ValueError("backup destination already exists")
    parent = requested.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise ValueError("backup destination directory does not exist") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or parent.is_symlink():
        raise ValueError("backup destination directory is unsafe")
    if _contains_symlink_component(parent):
        raise ValueError("backup destination contains a symbolic link")
    resolved_parent = parent.resolve(strict=True)
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o022:
        raise ValueError("backup destination directory must be private and owned by the current user")
    return resolved_parent / requested.name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--archive", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--archive", required=True)
    extract.add_argument("--destination", required=True)
    output = commands.add_parser("output-path")
    output.add_argument("--output", required=True)
    output.add_argument("--filename", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate":
            validate_archive(arguments.archive)
        elif arguments.command == "extract":
            extract_archive(arguments.archive, arguments.destination)
        else:
            print(safe_output_path(arguments.output, arguments.filename))
    except (UnsafeArchive, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
