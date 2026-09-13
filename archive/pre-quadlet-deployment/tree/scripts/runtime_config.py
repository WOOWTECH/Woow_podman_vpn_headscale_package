#!/usr/bin/env python3
"""Render runtime configs and select a Headscale URL from ngrok's API.

The dotenv parser in this module treats its input only as data. It does not
perform shell expansion, interpolation, or command execution.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
HEADSCALE_TEMPLATE = ROOT / "config/headscale/config.template.yaml"
HEADPLANE_TEMPLATE = ROOT / "config/headplane/config.template.yaml"

COMPOSE_PROJECT_NAME = "woow_headscale"

ENV_KEYS = {
    "COMPOSE_PROJECT_NAME",
    "SERVER_URL",
    "LOG_LEVEL",
    "IPV4_PREFIX",
    "IPV6_PREFIX",
    "MAGIC_DNS_BASE_DOMAIN",
    "CREATE_DEFAULT_USER",
    "NGROK_ENABLED",
    "NGROK_AUTHTOKEN",
    "NGROK_MODE",
    "NGROK_DOMAIN",
    "HEADSCALE_BIND_ADDR",
    "HEADSCALE_PORT",
    "HEADSCALE_METRICS_BIND_ADDR",
    "HEADSCALE_METRICS_PORT",
    "HEADPLANE_BIND_ADDR",
    "HEADPLANE_PORT",
    "HEADSCALE_HOST_BIND_ADDR",
    "HEADSCALE_HOST_PORT",
    "HEADSCALE_METRICS_HOST_BIND_ADDR",
    "HEADSCALE_METRICS_HOST_PORT",
    "HEADPLANE_HOST_BIND_ADDR",
    "HEADPLANE_HOST_PORT",
    "HEADPLANE_COOKIE_SECURE",
}
HOST_DEFAULTS = {
    "HEADSCALE_HOST_BIND_ADDR": "0.0.0.0",
    "HEADSCALE_HOST_PORT": "28080",
    "HEADSCALE_METRICS_HOST_BIND_ADDR": "127.0.0.1",
    "HEADSCALE_METRICS_HOST_PORT": "29090",
    "HEADPLANE_HOST_BIND_ADDR": "127.0.0.1",
    "HEADPLANE_HOST_PORT": "23000",
}
REQUIRED_KEYS = ENV_KEYS - {"HEADPLANE_COOKIE_SECURE", *HOST_DEFAULTS}
ENV_LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
HOST_LABEL = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII
)
SAFE_TOKEN = re.compile(r"[A-Za-z0-9._~-]+")
LOG_LEVELS = {"trace", "debug", "info", "warn", "error"}


def parse_env(path):
    """Parse a strict KEY=value dotenv file without shell semantics."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("unable to read the environment file") from exc

    values = {}
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE.fullmatch(line)
        if not match:
            raise ValueError(f"malformed environment line {number}")
        key, value = match.groups()
        if key not in ENV_KEYS:
            raise ValueError(f"unknown environment key on line {number}")
        if key in values:
            raise ValueError(f"duplicate environment key on line {number}")
        if any(character in value for character in ("\x00", "`", "$(", "${", "\\")):
            raise ValueError(f"unsafe environment value for {key}")
        values[key] = value

    missing = sorted(REQUIRED_KEYS - values.keys())
    if missing:
        raise ValueError("missing required environment keys: " + ", ".join(missing))
    for key, value in HOST_DEFAULTS.items():
        values.setdefault(key, value)
    return values


def _boolean(value, key):
    if value not in {"true", "false"}:
        raise ValueError(f"{key} must be true or false")
    return value == "true"


def _port(value, key):
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{key} must be a numeric port")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"{key} must be between 1 and 65535")
    return port


def _ip_address(value, key):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ValueError(f"{key} must be an IP address") from exc


def _network(value, version, key):
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ValueError(f"{key} must be a canonical network prefix") from exc
    if network.version != version:
        raise ValueError(f"{key} has the wrong IP version")
    return str(network)


def _domain(value, key, allow_empty=False):
    if allow_empty and value == "":
        return ""
    if not value or len(value) > 253 or value.endswith("."):
        raise ValueError(f"{key} must be a valid domain name")
    lowered = value.lower()
    if any(not HOST_LABEL.fullmatch(label) for label in lowered.split(".")):
        raise ValueError(f"{key} must be a valid domain name")
    return lowered


def _valid_url_hostname(hostname):
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return bool(
            hostname
            and len(hostname) <= 253
            and not hostname.endswith(".")
            and all(HOST_LABEL.fullmatch(label) for label in hostname.split("."))
        )


def _server_url(value, key="SERVER_URL"):
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"{key} must be a non-empty HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{key} must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not _valid_url_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{key} must be an origin URL without credentials, path, query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{key} contains an invalid port")
    return value.rstrip("/")


def _validate(values, server_url=None):
    rendered = dict(values)
    if values["COMPOSE_PROJECT_NAME"] != COMPOSE_PROJECT_NAME:
        raise ValueError(f"COMPOSE_PROJECT_NAME must be {COMPOSE_PROJECT_NAME}")
    rendered["SERVER_URL"] = _server_url(server_url if server_url is not None else values["SERVER_URL"])

    if values["LOG_LEVEL"] not in LOG_LEVELS:
        raise ValueError("LOG_LEVEL must be trace, debug, info, warn, or error")
    rendered["IPV4_PREFIX"] = _network(values["IPV4_PREFIX"], 4, "IPV4_PREFIX")
    rendered["IPV6_PREFIX"] = _network(values["IPV6_PREFIX"], 6, "IPV6_PREFIX")
    rendered["MAGIC_DNS_BASE_DOMAIN"] = _domain(values["MAGIC_DNS_BASE_DOMAIN"], "MAGIC_DNS_BASE_DOMAIN")

    _boolean(values["CREATE_DEFAULT_USER"], "CREATE_DEFAULT_USER")
    ngrok_enabled = _boolean(values["NGROK_ENABLED"], "NGROK_ENABLED")
    if values["NGROK_MODE"] not in {"http", "tcp"}:
        raise ValueError("NGROK_MODE must be http or tcp")
    rendered["NGROK_DOMAIN"] = _domain(values["NGROK_DOMAIN"], "NGROK_DOMAIN", allow_empty=True)
    if values["NGROK_MODE"] == "tcp" and rendered["NGROK_DOMAIN"]:
        raise ValueError("NGROK_DOMAIN cannot be used with NGROK_MODE=tcp")
    if ngrok_enabled and not SAFE_TOKEN.fullmatch(values["NGROK_AUTHTOKEN"]):
        raise ValueError("NGROK_AUTHTOKEN is required and contains invalid characters")
    if values["NGROK_AUTHTOKEN"] and not SAFE_TOKEN.fullmatch(values["NGROK_AUTHTOKEN"]):
        raise ValueError("NGROK_AUTHTOKEN contains invalid characters")

    internal_address_keys = (
        "HEADSCALE_BIND_ADDR",
        "HEADSCALE_METRICS_BIND_ADDR",
        "HEADPLANE_BIND_ADDR",
    )
    host_address_keys = (
        "HEADSCALE_HOST_BIND_ADDR",
        "HEADSCALE_METRICS_HOST_BIND_ADDR",
        "HEADPLANE_HOST_BIND_ADDR",
    )
    for key in internal_address_keys:
        rendered[key] = _ip_address(values[key], key)
    for key in host_address_keys:
        address = _ip_address(values[key], key)
        if ipaddress.ip_address(address).version != 4:
            raise ValueError(f"{key} must be an IPv4 address")
        rendered[key] = address
    for key in ("HEADSCALE_BIND_ADDR", "HEADSCALE_METRICS_BIND_ADDR"):
        if ":" in rendered[key]:
            rendered[key] = f"[{rendered[key]}]"
    port_keys = (
        "HEADSCALE_PORT",
        "HEADSCALE_METRICS_PORT",
        "HEADPLANE_PORT",
        "HEADSCALE_HOST_PORT",
        "HEADSCALE_METRICS_HOST_PORT",
        "HEADPLANE_HOST_PORT",
    )
    for key in port_keys:
        rendered[key] = str(_port(values[key], key))
    cookie_secure = _boolean(
        values.get("HEADPLANE_COOKIE_SECURE", "false"), "HEADPLANE_COOKIE_SECURE"
    )
    rendered["HEADPLANE_COOKIE_SECURE"] = "true" if cookie_secure else "false"

    host = urlsplit(rendered["SERVER_URL"]).hostname.lower().rstrip(".")
    magic_domain = rendered["MAGIC_DNS_BASE_DOMAIN"]
    if host == magic_domain or host.endswith("." + magic_domain):
        raise ValueError("SERVER_URL hostname must not equal or be a subdomain of the MagicDNS base domain")

    return rendered


def _render_template(template_path, replacements):
    try:
        content = Path(template_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("unable to read a configuration template") from exc
    for key, value in replacements.items():
        content = content.replace("{{" + key + "}}", value)
    unresolved = re.findall(r"{{[A-Z0-9_]+}}", content)
    if unresolved:
        raise ValueError("configuration template contains unresolved placeholders")
    return content


def _secure_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("runtime output directories must not be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("runtime output path is not a directory")
    os.chmod(path, 0o700)


def _secure_write(path, content):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("runtime configuration files must not be symbolic links")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def initialize_extra_records(output_dir):
    """Atomically create Headscale's empty extra-records file without following links."""
    output_dir = Path(output_dir)
    headscale_dir = output_dir / "headscale"
    for directory in (output_dir, headscale_dir):
        _secure_directory(directory)

    directory_fd = os.open(headscale_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = ".extra-records-" + secrets.token_hex(12)
    descriptor = None
    try:
        try:
            target_status = os.stat(
                "extra_records.json", dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            target_status = None
        if target_status is not None and stat.S_ISLNK(target_status.st_mode):
            raise ValueError("extra records file must not be a symbolic link")
        if target_status is not None and not stat.S_ISREG(target_status.st_mode):
            raise ValueError("extra records file must be a safe regular file")
        try:
            descriptor = os.open(
                "extra_records.json", os.O_PATH | os.O_NOFOLLOW, dir_fd=directory_fd
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("extra records file must be a safe regular file") from exc
        else:
            target_status = os.fstat(descriptor)
            os.close(descriptor)
            descriptor = None
            if not stat.S_ISREG(target_status.st_mode):
                raise ValueError("extra records file must be a safe regular file")
            return
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.write(descriptor, b"[]\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            "extra_records.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def validated_env(env_path):
    """Return the complete canonical environment after strict validation."""
    return _validate(parse_env(env_path))


def compose_project_name(env_path):
    """Return the one validated Compose project name."""
    return validated_env(env_path)["COMPOSE_PROJECT_NAME"]


def render_runtime_configs(env_path, output_dir, server_url=None):
    """Validate inputs and render Headscale and Headplane runtime YAML."""
    values = _validate(parse_env(env_path), server_url=server_url)
    output_dir = Path(output_dir)
    headscale_dir = output_dir / "headscale"
    headplane_dir = output_dir / "headplane"
    for directory in (output_dir, headscale_dir, headplane_dir):
        _secure_directory(directory)

    outputs = {
        "headscale": headscale_dir / "config.yaml",
        "headplane": headplane_dir / "config.yaml",
    }
    _secure_write(outputs["headscale"], _render_template(HEADSCALE_TEMPLATE, values))
    _secure_write(outputs["headplane"], _render_template(HEADPLANE_TEMPLATE, values))
    return outputs


def select_ngrok_public_url(payload, mode, domain=None):
    """Select one ngrok tunnel URL and normalize TCP for Headscale."""
    if mode not in {"http", "tcp"}:
        raise ValueError("ngrok mode must be http or tcp")
    expected_domain = _domain(domain, "ngrok domain") if domain else None
    if not isinstance(payload, dict) or not isinstance(payload.get("tunnels"), list):
        raise ValueError("ngrok API JSON must contain a tunnels list")

    candidates = []
    for tunnel in payload["tunnels"]:
        if not isinstance(tunnel, dict) or not isinstance(tunnel.get("public_url"), str):
            continue
        public_url = tunnel["public_url"]
        try:
            parsed = urlsplit(public_url)
            parsed_port = parsed.port
        except ValueError:
            continue
        if mode == "http":
            if parsed.scheme not in {"http", "https"}:
                continue
            try:
                normalized = _server_url(public_url, "ngrok public URL")
            except ValueError:
                continue
        else:
            if (
                parsed.scheme != "tcp"
                or not parsed.hostname
                or not _valid_url_hostname(parsed.hostname)
                or parsed_port is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                continue
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            normalized = f"http://{host}:{parsed_port}"
        if expected_domain and parsed.hostname.lower().rstrip(".") != expected_domain:
            continue
        candidates.append(normalized)

    if not candidates:
        raise ValueError("no matching ngrok tunnel was found")
    if len(candidates) != 1:
        raise ValueError("ambiguous ngrok tunnels; expected exactly one match")
    return candidates[0]


def _read_api_json(path):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with Path(path).open("r", encoding="utf-8") as source:
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("unable to read valid ngrok API JSON") from exc


def _argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="subcommand", required=True)

    render = subcommands.add_parser("render", help="render validated runtime YAML configurations")
    render.add_argument("--env-file", default=".env", help="strict dotenv input (default: .env)")
    render.add_argument("--output-dir", default="runtime", help="runtime output directory")
    render.add_argument("--server-url", help="validated discovered URL overriding SERVER_URL")

    project_name = subcommands.add_parser(
        "project-name", help="print the strictly validated Compose project name"
    )
    project_name.add_argument("--env-file", default=".env", help="strict dotenv input (default: .env)")

    extra_records = subcommands.add_parser(
        "init-extra-records", help="securely initialize Headscale's extra-records JSON"
    )
    extra_records.add_argument("--output-dir", default="runtime", help="runtime output directory")

    ngrok = subcommands.add_parser("ngrok-url", help="select a public URL from ngrok API JSON")
    ngrok.add_argument("--api-json", required=True, help="ngrok API JSON file, or - for stdin")
    ngrok.add_argument("--mode", required=True, choices=("http", "tcp"))
    ngrok.add_argument("--domain", help="fixed domain that the selected tunnel must match")
    return parser


def main(argv=None):
    parser = _argument_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.subcommand == "render":
            render_runtime_configs(arguments.env_file, arguments.output_dir, arguments.server_url)
        elif arguments.subcommand == "project-name":
            print(compose_project_name(arguments.env_file))
        elif arguments.subcommand == "init-extra-records":
            initialize_extra_records(arguments.output_dir)
        else:
            payload = _read_api_json(arguments.api_json)
            print(select_ngrok_public_url(payload, arguments.mode, arguments.domain))
    except ValueError as exc:
        parser.error(str(exc))
    except OSError:
        parser.error("unable to write runtime configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
