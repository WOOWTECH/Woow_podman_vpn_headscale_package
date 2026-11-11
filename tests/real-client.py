#!/usr/bin/env python3
"""Enroll an isolated userspace Tailscale client without touching host tailscaled."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import time


def hs(*args: str) -> str:
    return subprocess.check_output(["podman", "exec", "headscale", "headscale", *args], text=True)


def ts(socket: pathlib.Path, *args: str, timeout: int = 20) -> str:
    return subprocess.check_output(["tailscale", f"--socket={socket}", *args], text=True, timeout=timeout)


def login_server() -> str:
    value = os.environ.get("HEADSCALE_LOGIN_SERVER")
    if value:
        return value
    settings = pathlib.Path.home() / ".config/headscale/headscale.env"
    for line in settings.read_text().splitlines():
        if line.startswith("HEADSCALE_SERVER_URL="):
            return line.partition("=")[2]
    raise RuntimeError("HEADSCALE_SERVER_URL is absent from the installed settings")


def main() -> dict[str, object]:
    user = next(u for u in json.loads(hs("users", "list", "-o", "json")) if u["name"] == "default")
    key_info = json.loads(hs("preauthkeys", "create", "--user", str(user["id"]), "--expiration", "30m", "-o", "json"))
    key_id = key_info["id"]
    key = key_info["key"]
    hostname = "uat-headscale-" + os.urandom(4).hex()
    result: dict[str, object] = {"test": "isolated real Tailscale client enrollment", "passed": False, "hostname": hostname}
    node_id: int | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="headscale-client-") as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o700)
            key_path = root / "authkey"
            key_path.write_text(key + "\n")
            key_path.chmod(0o600)
            socket = root / "tailscaled.sock"
            with (root / "tailscaled.log").open("wb") as logfile:
                daemon = subprocess.Popen(
                    ["tailscaled", f"--socket={socket}", "--state=mem:", "--tun=userspace-networking", "--no-logs-no-support"],
                    stdout=logfile, stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 15
                    while not socket.exists() and time.monotonic() < deadline:
                        if daemon.poll() is not None:
                            raise RuntimeError("isolated tailscaled exited")
                        time.sleep(0.2)
                    if not socket.exists():
                        raise TimeoutError("isolated tailscaled socket absent")
                    ts(socket, "up", f"--login-server={login_server()}", f"--authkey=file:{key_path}",
                       f"--hostname={hostname}", "--accept-dns=false", timeout=60)
                    status = json.loads(ts(socket, "status", "--json"))
                    result["client_backend_state"] = status.get("BackendState")
                    result["client_ips"] = status.get("Self", {}).get("TailscaleIPs", [])
                    nodes = json.loads(hs("nodes", "list", "-o", "json"))
                    if isinstance(nodes, dict):
                        nodes = nodes.get("nodes", [])
                    node = next(n for n in nodes if n.get("name", "").startswith(hostname))
                    node_id = int(node["id"])
                    result["server_node_id"] = node_id
                    result["server_node_name"] = node.get("name")
                    result["passed"] = status.get("BackendState") == "Running" and bool(result["client_ips"])
                finally:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait()
    finally:
        if node_id is not None:
            hs("nodes", "delete", "--identifier", str(node_id), "--force")
        hs("preauthkeys", "expire", "--id", str(key_id))
    return result


if __name__ == "__main__":
    try:
        outcome = main()
    except Exception as error:
        outcome = {"test": "isolated real Tailscale client enrollment", "passed": False,
                   "error": type(error).__name__ + ": " + str(error)[:200]}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["passed"] else 1)
