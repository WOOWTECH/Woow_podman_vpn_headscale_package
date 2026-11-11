#!/usr/bin/env python3
"""Exercise control and WireGuard data paths with two disposable userspace clients."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def hs(*args: str) -> str:
    return subprocess.check_output(["podman", "exec", "headscale", "headscale", *args], text=True)


def ts(socket: Path, *args: str, timeout: int = 60) -> str:
    return subprocess.check_output(["tailscale", f"--socket={socket}", *args], text=True, timeout=timeout)


def server_url() -> str:
    for line in (Path.home() / ".config/headscale/headscale.env").read_text().splitlines():
        if line.startswith("HEADSCALE_SERVER_URL="):
            return line.partition("=")[2]
    raise RuntimeError("HEADSCALE_SERVER_URL is missing")


def start_client(root: Path, name: str, key: str, url: str) -> tuple[subprocess.Popen[bytes], Path, list[str]]:
    root.mkdir(mode=0o700)
    key_path = root / "authkey"
    key_path.write_text(key + "\n")
    key_path.chmod(0o600)
    socket = root / "tailscaled.sock"
    with (root / "tailscaled.log").open("wb") as logfile:
        daemon = subprocess.Popen(
            ["tailscaled", f"--socket={socket}", "--state=mem:",
             "--tun=userspace-networking", "--no-logs-no-support"],
            stdout=logfile, stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 15
        while not socket.exists() and time.monotonic() < deadline:
            if daemon.poll() is not None:
                raise RuntimeError(f"{name}: userspace tailscaled exited")
            time.sleep(0.2)
        if not socket.exists():
            raise TimeoutError(f"{name}: userspace tailscaled socket absent")
        ts(socket, "up", f"--login-server={url}", f"--authkey=file:{key_path}",
           f"--hostname={name}", "--accept-dns=false")
        status = json.loads(ts(socket, "status", "--json"))
        if status.get("BackendState") != "Running":
            raise RuntimeError(f"{name}: backend did not enter Running")
        ips = status.get("Self", {}).get("TailscaleIPs", [])
        if len(ips) < 2:
            raise RuntimeError(f"{name}: IPv4/IPv6 assignment incomplete")
        return daemon, socket, ips
    except BaseException:
        daemon.terminate()
        daemon.wait(timeout=10)
        raise


def peer_ping(socket: Path, peer_ip: str) -> str:
    last_error = ""
    for _ in range(6):
        cmd = ["tailscale", f"--socket={socket}", "ping", "--tsmp", "--c=2",
               "--until-direct=false", "--timeout=5s", peer_ip]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=20)
        if result.returncode == 0:
            return result.stdout.strip().splitlines()[-1]
        last_error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no reply"
        time.sleep(2)
    raise RuntimeError(f"WireGuard TSMP ping failed: {last_error[:120]}")


def wait_public_health(url: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url + "/health", headers={"ngrok-skip-browser-warning": "true"})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2)
    raise TimeoutError("public ngrok /health did not recover")


def main(restart_tunnel: bool) -> dict[str, object]:
    url = server_url()
    user = next(u for u in json.loads(hs("users", "list", "-o", "json")) if u["name"] == "default")
    suffix = os.urandom(4).hex()
    names = [f"uat-peer-a-{suffix}", f"uat-peer-b-{suffix}"]
    key_ids: list[int] = []
    daemons: list[subprocess.Popen[bytes]] = []
    result: dict[str, object] = {"test": "two isolated Headscale peers via ngrok", "server_url": url,
                                 "passed": False, "tunnel_restarted": False}
    try:
        with tempfile.TemporaryDirectory(prefix="headscale-peers-") as tmp:
            clients = []
            for i, name in enumerate(names):
                info = json.loads(hs("preauthkeys", "create", "--user", str(user["id"]),
                                     "--expiration", "30m", "-o", "json"))
                key_ids.append(info["id"])
                daemon, socket, ips = start_client(Path(tmp) / str(i), name, info["key"], url)
                daemons.append(daemon)
                clients.append((socket, ips))
            result["peer_ips"] = [ips for _, ips in clients]
            result["ping_before"] = peer_ping(clients[0][0], clients[1][1][0])
            if restart_tunnel:
                subprocess.run(["systemctl", "--user", "restart", "headscale-ngrok.service"], check=True)
                wait_public_health(url)
                result["tunnel_restarted"] = True
                result["ping_after"] = peer_ping(clients[0][0], clients[1][1][0])
            result["passed"] = True
    finally:
        for daemon in daemons:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
        nodes = json.loads(hs("nodes", "list", "-o", "json")) or []
        if isinstance(nodes, dict):
            nodes = nodes.get("nodes") or []
        for node in nodes:
            if node.get("name") in names:
                hs("nodes", "delete", "--identifier", str(node["id"]), "--force")
        for key_id in key_ids:
            hs("preauthkeys", "expire", "--id", str(key_id))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-tunnel", action="store_true")
    options = parser.parse_args()
    try:
        outcome = main(options.restart_tunnel)
    except Exception as error:
        outcome = {"test": "two isolated Headscale peers via ngrok", "passed": False,
                   "error": type(error).__name__ + ": " + str(error)[:200]}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["passed"] else 1)
