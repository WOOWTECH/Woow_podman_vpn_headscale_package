#!/usr/bin/env python3
"""Enroll disposable peers on this host and an SSH host through the public URL.

Requires Paramiko and an SSH login to a same-architecture Linux host. It copies the
locally installed Tailscale binaries to a private remote /tmp directory, runs both
daemons in userspace mode, and removes the nodes, keys and remote files afterward.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time

import paramiko


spec = importlib.util.spec_from_file_location("real_peers", Path(__file__).with_name("real-peers.py"))
assert spec and spec.loader
peers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peers)


def remote(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdout.channel.settimeout(timeout)
    output = stdout.read().decode()
    error = stderr.read().decode()
    status = stdout.channel.recv_exit_status()
    if status:
        raise RuntimeError(f"remote command failed ({status}): {error[-200:]}")
    return output.strip()


def upload(client: paramiko.SSHClient, source: Path, target: str) -> None:
    """Stream to a private remote file over an SSH exec channel (no SFTP needed)."""
    stdin, stdout, stderr = client.exec_command(
        "umask 077; cat > " + shlex.quote(target), timeout=120
    )
    try:
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                stdin.channel.sendall(chunk)
        stdin.channel.shutdown_write()
        if stdout.channel.recv_exit_status():
            raise RuntimeError("remote upload failed: " + stderr.read().decode()[-200:])
    finally:
        stdin.close()


def run(host: str, user: str, force_derp: bool) -> dict[str, object]:
    url = peers.server_url()
    password = getpass.getpass(f"{user}@{host} SSH password: ")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.connect(host, username=user, password=password, timeout=10,
                   look_for_keys=False, allow_agent=False)
    del password
    suffix = os.urandom(4).hex()
    names = [f"uat-local-{suffix}", f"uat-remote-{suffix}"]
    key_ids: list[int] = []
    local_daemon: subprocess.Popen[bytes] | None = None
    remote_pid: int | None = None
    remote_dir = ""
    result: dict[str, object] = {"test": "cross-host Headscale peers via ngrok",
                                 "server_url": url, "force_derp": force_derp, "passed": False}
    try:
        if remote(client, "uname -m") != os.uname().machine:
            raise RuntimeError("remote host architecture differs from local Tailscale binaries")
        remote_dir = remote(client, "mktemp -d /tmp/headscale-peer.XXXXXX")
        if not remote_dir.startswith("/tmp/headscale-peer."):
            raise RuntimeError("remote temp directory was unexpected")
        for local, name in [(shutil.which("tailscale"), "tailscale"),
                            (shutil.which("tailscaled"), "tailscaled")]:
            if not local:
                raise RuntimeError(f"local {name} binary is missing")
            upload(client, Path(local), remote_dir + "/" + name)
            remote(client, "chmod 700 " + shlex.quote(remote_dir + "/" + name))

        user_info = next(u for u in json.loads(peers.hs("users", "list", "-o", "json"))
                         if u["name"] == "default")
        with tempfile.TemporaryDirectory(prefix="headscale-cross-host-") as tmp:
            keys: list[str] = []
            for i in range(2):
                info = json.loads(peers.hs("preauthkeys", "create", "--user",
                                           str(user_info["id"]), "--expiration", "30m", "-o", "json"))
                key_ids.append(info["id"])
                keys.append(info["key"])
            old_debug = os.environ.get("TS_DEBUG_ALWAYS_USE_DERP")
            if force_derp:
                os.environ["TS_DEBUG_ALWAYS_USE_DERP"] = "1"
            try:
                local_daemon, socket, local_ips = peers.start_client(
                    Path(tmp) / "local", names[0], keys[0], url)
            finally:
                if old_debug is None:
                    os.environ.pop("TS_DEBUG_ALWAYS_USE_DERP", None)
                else:
                    os.environ["TS_DEBUG_ALWAYS_USE_DERP"] = old_debug
            key_path = Path(tmp) / "remote-authkey"
            key_path.write_text(keys[1] + "\n")
            key_path.chmod(0o600)
            upload(client, key_path, remote_dir + "/authkey")
            qdir = shlex.quote(remote_dir)
            prefix = "TS_DEBUG_ALWAYS_USE_DERP=1 " if force_derp else ""
            remote_pid = int(remote(client, f"cd {qdir}; ({prefix}./tailscaled "
                                            f"--socket={qdir}/tailscaled.sock --state=mem: "
                                            "--tun=userspace-networking --no-logs-no-support "
                                            "> tailscaled.log 2>&1 < /dev/null & echo $!)"))
            time.sleep(2)
            remote(client, f"{qdir}/tailscale --socket={qdir}/tailscaled.sock up "
                           f"--login-server={shlex.quote(url)} --authkey=file:{qdir}/authkey "
                           f"--hostname={names[1]} --accept-dns=false", timeout=75)
            status = json.loads(remote(client, f"{qdir}/tailscale "
                                               f"--socket={qdir}/tailscaled.sock status --json"))
            remote_ips = status.get("Self", {}).get("TailscaleIPs", [])
            if status.get("BackendState") != "Running" or len(remote_ips) < 2:
                raise RuntimeError("remote Tailscale client did not reach Running with IPv4/IPv6")
            result["peer_ips"] = [local_ips, remote_ips]
            result["local_to_remote_tsmp"] = peers.peer_ping(socket, remote_ips[0])
            result["remote_to_local_tsmp"] = remote(
                client, f"{qdir}/tailscale --socket={qdir}/tailscaled.sock ping "
                        f"--tsmp --c=2 --until-direct=false --timeout=5s {local_ips[0]}", timeout=30
            ).splitlines()[-1]
            disco = peers.ts(socket, "ping", "--c=3", "--until-direct=false",
                             "--timeout=5s", remote_ips[0], timeout=30)
            result["disco_routes"] = disco.strip().splitlines()
            if force_derp and not any("via DERP(" in line for line in result["disco_routes"]):
                raise RuntimeError("forced-DERP test did not report a DERP route")
            result["passed"] = True
    finally:
        if local_daemon:
            local_daemon.terminate()
            try:
                local_daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                local_daemon.kill()
                local_daemon.wait()
        if remote_dir:
            try:
                if remote_pid:
                    remote(client, f"kill {remote_pid} 2>/dev/null || true")
                    time.sleep(1)
                remote(client, f"rm -rf -- {shlex.quote(remote_dir)}")
            except Exception:
                pass
        client.close()
        nodes = json.loads(peers.hs("nodes", "list", "-o", "json")) or []
        if isinstance(nodes, dict):
            nodes = nodes.get("nodes") or []
        for node in nodes:
            if node.get("name") in names:
                peers.hs("nodes", "delete", "--identifier", str(node["id"]), "--force")
        for key_id in key_ids:
            peers.hs("preauthkeys", "expire", "--id", str(key_id))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--force-derp", action="store_true")
    options = parser.parse_args()
    try:
        outcome = run(options.host, options.user, options.force_derp)
    except Exception as error:
        outcome = {"test": "cross-host Headscale peers via ngrok", "passed": False,
                   "error": type(error).__name__ + ": " + str(error)[:200]}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["passed"] else 1)
