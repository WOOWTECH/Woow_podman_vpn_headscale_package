# External Access Deep-Dive / 外部連線深度分析

How to let devices on the internet reach a Headscale control plane running inside a NAT-ed K3s cluster.

讓網際網路上的裝置連上位於 NAT 後 K3s 叢集內的 Headscale 控制平面。

---

## The Problem / 問題

The Tailscale control protocol (TS2021) is **not plain HTTP**:

1. The client sends `POST /ts2021` with `Upgrade: tailscale-control-protocol` (a non-standard upgrade header, and POST instead of the WebSocket-standard GET).
2. On `101 Switching Protocols`, a **Noise IK** cryptographic handshake runs over the hijacked connection.
3. HTTP/2 is then multiplexed **inside** the Noise-encrypted channel.

Any middlebox that inspects, validates, or rewrites HTTP upgrade semantics will break step 1 — the client sees `500 Internal Server Error` on `/machine/register`, and Headscale logs:

```
WRN no upgrade header in TS2021 request. If headscale is behind a reverse
proxy, make sure it is configured to pass WebSockets through.
```

## Compatibility Matrix / 相容性總表

Tested against a live deployment (2026-07):

| Path | Result | Evidence |
|------|--------|----------|
| Cloudflare Tunnel (HTTP) | ❌ **broken** | CF strips `Upgrade` on POST — [cloudflared#883](https://github.com/cloudflare/cloudflared/issues/883), [#990](https://github.com/cloudflare/cloudflared/issues/990); [official Headscale docs](https://headscale.net/stable/ref/integration/reverse-proxy/) confirm |
| ngrok HTTP tunnel | ✅ **works** | Client reached `Running` state, node registered, DERP connected |
| ngrok TCP tunnel | ✅ works (`http://` URL) | Raw passthrough; random `N.tcp.ngrok.io:port` ⇒ no valid TLS cert possible |
| In-cluster service URL | ✅ works | `http://headscale.<ns>.svc.cluster.local:8080` for proxy pods |
| Traefik (ingress) | ✅ expected | Native upgrade passthrough; force HTTP/1.1 ALPN if issues ([traefik#12609](https://github.com/traefik/traefik/issues/12609)) |
| Nginx / Caddy / Apache | ✅ documented | See official reverse-proxy configs below |

## Option A — Router Port-Forward + Traefik (Production)

```
Internet ──443──> Router ──443──> K3s node (Traefik LB) ──> Headscale svc :8080
```

1. Create a cert-manager `ClusterIssuer` with Cloudflare DNS-01 (no inbound port needed for issuance).
2. `Certificate` for `vpn-<tenant>.example.com` → Secret.
3. Traefik `IngressRoute` (entryPoint `websecure`, TLS secret from step 2).
4. Cloudflare DNS: **A record, DNS-only (grey cloud)** → your public IP.
5. Router: forward TCP 443 → a K3s node running Traefik.

> If Traefik negotiates HTTP/2 and the upgrade fails, pin ALPN to HTTP/1.1 with a `TLSOption` (`alpnProtocols: ["http/1.1"]`).

## Option B — ngrok (Fast Bootstrap, verified)

```bash
ngrok config add-authtoken <token>
ngrok http <headscale-clusterip>:8080
# → https://random-name.ngrok-free.app  (valid TLS, protocol passes through)
```

Client:

```bash
tailscale up --login-server=https://random-name.ngrok-free.app --authkey=<key>
```

Free-tier caveats: URL changes each restart, 1 GB/month bandwidth, interstitial page on browser traffic (does not affect the Tailscale client).

For a stable URL: ngrok paid plan with a reserved domain, or move to Option A.

## Option C — Raw TCP tunnels (Pinggy / bore / ngrok tcp)

Raw TCP passthrough always preserves the protocol, but the random hostname/port makes valid TLS impossible. Usable with `--login-server=http://…` for **testing only** (the inner noise handshake is still encrypted, but prefer HTTPS in production).

## Nginx reference config (official)

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
server {
    listen 443 ssl;
    server_name vpn-tenant.example.com;
    location / {
        proxy_pass http://headscale:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $server_name;
        proxy_buffering off;
    }
}
```

A successful `GET /health` proves only ordinary HTTP reachability; it does **not** exercise the non-WebSocket TS2021 Upgrade and Noise handshake. Validate a proxy by registering a real official Tailscale client and confirming that the node appears in Headscale. Preserve HTTP/1.1, both `Upgrade`/`Connection` headers, and disabled proxy buffering as shown above.

## Split Architecture (what this repo uses)

| Traffic | Path |
|---------|------|
| Headplane admin UI | Cloudflare Tunnel (plain HTTP — works) |
| Headscale VPN control plane | ngrok / router port-forward (protocol-safe path) |
| In-cluster proxy pods | Internal service URL (bypasses everything) |
| WireGuard data plane | Direct P2P or Tailscale public DERP relays |
