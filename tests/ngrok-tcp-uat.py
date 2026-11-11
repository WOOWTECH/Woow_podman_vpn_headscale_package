#!/usr/bin/env python3
"""Temporarily test ngrok TCP with two Tailscale peers, then restore HTTPS.

Prompts for an ngrok platform API key. Creates one short-lived unscoped agent
credential, uses the Podman endpoint slot while this host's HTTPS tunnel is
stopped, and removes both test resources in finally. Other hosts are untouched.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


repo = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ngrok_sync", repo / "scripts/ngrok-sync.py")
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
env_file = Path.home() / ".config/headscale/headscale.env"
agent_image = subprocess.check_output(
    ["podman", "inspect", "--format", "{{.ImageName}}", "headscale-ngrok"], text=True
).strip()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(key: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        "https://api.ngrok.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + key, "Ngrok-Version": "2",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response) if response.status != 204 else {}
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get("error_code", "unknown")
        except ValueError:
            code = "unknown"
        raise RuntimeError(f"ngrok API {method} failed: HTTP {error.code}, {code}") from None


def command(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed (exit {result.returncode})")
    return result.stdout.strip()


def live_tcp(container: str) -> str:
    end = time.monotonic() + 45
    while time.monotonic() < end:
        try:
            raw = command("podman", "exec", container, "wget", "-qO-", sync.API)
            url = sync.parse_public_url(json.loads(raw), "tcp")
            if url:
                return url
        except (RuntimeError, ValueError):
            pass
        running = subprocess.run(
            ["podman", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True, text=True
        )
        if running.returncode == 0 and running.stdout.strip() == "false":
            break
        time.sleep(2)
    logs = subprocess.run(["podman", "logs", "--tail", "100", container],
                          capture_output=True, text=True)
    codes = sorted(set(re.findall(r"ERR_NGROK_[0-9]+", logs.stdout + logs.stderr)))
    details = logs.stdout + logs.stderr
    reason = ""
    for pattern, label in ((r"unknown flag", "unsupported CLI flag"),
                           (r"payment|credit.card", "payment verification required"),
                           (r"reserve.*tcp|tcp.*reserv", "TCP address reservation required"),
                           (r"endpoint.*limit|too many endpoint", "endpoint limit")):
        if re.search(pattern, details, flags=re.IGNORECASE):
            reason = label
            break
    suffix = ",".join(codes) or reason
    raise RuntimeError("TCP endpoint unavailable" + (" (" + suffix + ")" if suffix else ""))


def main() -> dict[str, object]:
    old_env = env_file.read_text()
    old_url = sync.read_env(env_file)["HEADSCALE_SERVER_URL"]
    if sync.read_env(env_file).get("HEADSCALE_NGROK_ENABLED") != "true":
        raise RuntimeError("production HTTPS ngrok mode is not enabled")
    key = getpass.getpass("ngrok platform API key: ")
    credential_id = ""
    secret_name = "headscale-ngrok-tcp-uat-" + os.urandom(4).hex()
    container = "headscale-ngrok-tcp-uat"
    stopped = False
    result: dict[str, object] = {"test": "ngrok TCP + two real Tailscale peers",
                                 "passed": False, "https_restored": False}
    cleanup_errors: list[str] = []
    try:
        credential = api(key, "POST", "/credentials", {"description": "Headscale TCP UAT, temporary"})
        credential_id = credential["id"]
        token = credential["token"]
        command("podman", "secret", "create", secret_name, "-", input_text=token + "\n")
        del token
        command("systemctl", "--user", "stop", "headscale-ngrok.service")
        stopped = True
        command("podman", "run", "-d", "--pull=never", "--name", container,
                "--network", "headscale-net", "--security-opt", "no-new-privileges",
                "--secret", f"{secret_name},type=env,target=NGROK_AUTHTOKEN",
                agent_image, "tcp", "headscale-entrance:8081", "--log", "stdout")
        url = live_tcp(container)
        result["tcp_url"] = url
        with opener.open(url + "/health", timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("public TCP /health is not 200")
        result["tcp_health"] = 200
        # The ephemeral agent forwards to the same private path filter. Disable
        # only this host's Quadlet ngrok unit while the TCP client test runs.
        command(str(repo / "scripts/install.sh"), "--no-build", "--set",
                "HEADSCALE_NGROK_ENABLED=false", "--set", "HEADSCALE_SERVER_URL=" + url)
        peers = subprocess.run([sys.executable, str(repo / "tests/real-peers.py")],
                               capture_output=True, text=True, timeout=180)
        try:
            peer_result = json.loads(peers.stdout.splitlines()[-1])
        except (IndexError, ValueError):
            peer_result = {"passed": False, "error": "peer test produced no JSON"}
        result["peers"] = peer_result
        result["passed"] = peers.returncode == 0 and bool(peer_result.get("passed"))
    except Exception as error:
        result["error"] = type(error).__name__ + ": " + str(error)[:200]
    finally:
        for args in (["podman", "stop", "--time", "10", container],
                     ["podman", "rm", "--force", container],
                     ["podman", "secret", "rm", secret_name]):
            try:
                subprocess.run(args, capture_output=True, text=True, timeout=30)
            except Exception as error:
                cleanup_errors.append(type(error).__name__ + " during local cleanup")
        if credential_id:
            try:
                api(key, "DELETE", "/credentials/" + credential_id)
            except Exception as error:
                cleanup_errors.append(str(error))
        if stopped:
            try:
                sync.atomic_replace(env_file, old_env)
                restore = subprocess.run([str(repo / "scripts/install.sh"), "--no-build"],
                                         capture_output=True, text=True, timeout=240)
                restored_url = sync.read_env(env_file).get("HEADSCALE_SERVER_URL")
                active = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                         "headscale-ngrok.service"]).returncode == 0
                public_healthy = False
                if restore.returncode == 0 and restored_url == old_url and active:
                    for _ in range(15):
                        try:
                            with opener.open(old_url + "/health", timeout=5) as response:
                                public_healthy = response.status == 200
                                if public_healthy:
                                    break
                        except (urllib.error.URLError, TimeoutError):
                            pass
                        time.sleep(2)
                result["https_restored"] = (restore.returncode == 0 and restored_url == old_url and
                                            active and public_healthy)
                if not result["https_restored"]:
                    cleanup_errors.append("HTTPS restore failed; check user services and install logs")
            except Exception as error:
                cleanup_errors.append(type(error).__name__ + " during HTTPS restore")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result["passed"] = False
    return result


if __name__ == "__main__":
    try:
        outcome = main()
    except Exception as error:
        outcome = {"test": "ngrok TCP + two real Tailscale peers", "passed": False,
                   "error": type(error).__name__ + ": " + str(error)[:200]}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["passed"] else 1)
