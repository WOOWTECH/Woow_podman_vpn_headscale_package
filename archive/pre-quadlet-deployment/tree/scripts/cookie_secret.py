#!/usr/bin/env python3
"""Safely initialize and validate Headplane's fixed-length cookie secret."""

import argparse
import errno
import os
from pathlib import Path
import re
import secrets
import stat
import sys


VALID_SECRET = re.compile(rb"[A-Za-z0-9_-]{32}\Z")
LEGACY_SECRET = re.compile(rb"[0-9a-f]{64}(?:\n)?\Z")


class SecretError(ValueError):
    """Raised when an existing secret does not meet the storage contract."""


def read_private_regular(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SecretError("cookie secret must not be a symbolic link") from exc
        raise SecretError("cookie secret could not be opened safely") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretError("cookie secret must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise SecretError("cookie secret must have mode 600")
        chunks = []
        remaining = 66
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise SecretError("cookie secret has an invalid length or character")
        return b"".join(chunks), metadata
    finally:
        os.close(descriptor)


def write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def stage_secret(parent_fd: int, value: bytes) -> str:
    for _attempt in range(128):
        temporary_name = f".cookie-secret.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            os.fchmod(descriptor, 0o600)
            write_all(descriptor, value)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            os.unlink(temporary_name, dir_fd=parent_fd)
            raise
        os.close(descriptor)
        return temporary_name
    raise SecretError("could not allocate a temporary cookie secret file")


def open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def publish_new(path: Path, value: bytes) -> None:
    parent_fd = open_directory(path.parent)
    temporary_name = stage_secret(parent_fd, value)
    try:
        # A hard-link publication is atomic and, unlike replace, cannot overwrite a
        # file that appeared after the absence check.
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.fsync(parent_fd)
    finally:
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def replace_legacy(path: Path, value: bytes, original: os.stat_result) -> None:
    parent_fd = open_directory(path.parent)
    temporary_name = stage_secret(parent_fd, value)
    try:
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise SecretError("cookie secret changed during validation")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = ""
        os.fsync(parent_fd)
    finally:
        if temporary_name:
            os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def generated_secret() -> bytes:
    return secrets.token_hex(16).encode("ascii")


def validate_or_rotate(path: Path) -> None:
    value, metadata = read_private_regular(path)
    if VALID_SECRET.fullmatch(value):
        return
    if LEGACY_SECRET.fullmatch(value):
        replace_legacy(path, generated_secret(), metadata)
        return
    raise SecretError("cookie secret has an invalid length or character")


def ensure_cookie_secret(path: Path, legacy_path: Path | None = None) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        validate_or_rotate(path)
        return

    if legacy_path is not None:
        try:
            legacy_path.lstat()
        except FileNotFoundError:
            pass
        else:
            legacy_value, _metadata = read_private_regular(legacy_path)
            if VALID_SECRET.fullmatch(legacy_value):
                value = legacy_value
            elif LEGACY_SECRET.fullmatch(legacy_value):
                value = generated_secret()
            else:
                raise SecretError("cookie secret has an invalid length or character")
            publish_new(path, value)
            legacy_path.unlink()
            return

    publish_new(path, generated_secret())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--legacy", type=Path)
    args = parser.parse_args()
    try:
        ensure_cookie_secret(args.path, args.legacy)
    except (OSError, SecretError) as exc:
        print(f"cookie secret initialization failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
