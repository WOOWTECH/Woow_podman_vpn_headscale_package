#!/usr/bin/env python3
"""Render the bundled user systemd unit for one checkout."""

import argparse
import os
from pathlib import Path
import tempfile
import unicodedata


def render(source: Path, target: Path, project_text: str) -> None:
    if not Path(project_text).is_absolute():
        raise ValueError("project directory must be absolute")
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in project_text):
        raise ValueError("project directory contains unsupported whitespace or control characters")
    if any(character in "\\\"'" for character in project_text):
        raise ValueError("project directory contains characters unsafe for an unquoted systemd value")

    escaped_project = project_text.replace("%", "%%")
    template = source.read_text(encoding="utf-8")
    placeholder_count = template.count("@PROJECT_DIR@")
    if placeholder_count not in (0, 3):
        raise ValueError("systemd unit template has an unexpected project placeholder count")
    content = template.replace("@PROJECT_DIR@", escaped_project)

    descriptor, temporary = tempfile.mkstemp(prefix=".woow_headscale.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("project")
    arguments = parser.parse_args()
    try:
        render(arguments.source, arguments.target, arguments.project)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
