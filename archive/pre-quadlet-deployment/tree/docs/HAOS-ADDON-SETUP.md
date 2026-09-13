# Home Assistant OS — Tailscale Add-on × Headscale / HAOS Tailscale Add-on 接入指南

Connect a Home Assistant OS (HAOS / Supervised) appliance to your self-hosted Headscale tailnet using the official community add-on ([hassio-addons/addon-tailscale](https://github.com/hassio-addons/addon-tailscale)).

> **HA Container / Core users:** the add-on requires the Supervisor. Use the tailscale sidecar / proxy-pod pattern from `manifests/vpn-proxy/` instead.

---

## 1. Recommended add-on configuration / 建議設定

Settings → Add-ons → Tailscale → Configuration:

```yaml
login_server: "https://<your-headscale-url>"
accept_dns: true
accept_routes: true
advertise_exit_node: false      # Tailscale-cloud feature semantics; keep off
advertise_connector: false      # NOT supported with Headscale
share_homeassistant: disabled   # Serve/Funnel NOT supported with Headscale
always_use_derp: false
userspace_networking: true
log_level: info
```

Options **not supported** when using Headscale: App Connector, Tailscale Serve/Funnel, admin-console key expiry management.

## 2. Registration flow / 註冊流程

The official add-on has **no `auth_key` option** — it uses the browser/URL flow:

1. Start the add-on.
2. Open the add-on **Log** tab. Look for:
   ```
   To authenticate, visit:
       https://<headscale-url>/register/hskey-authreq-XXXXXXXX
   ```
3. Register the node on the Headscale side:
   ```bash
   kubectl exec headscale-0 -n tenant-test -c headscale -- \
     headscale auth register --user default --auth-id hskey-authreq-XXXXXXXX
   # (or the deprecated alias: headscale nodes register --user default --key …)
   ```
4. The add-on connects within seconds; verify:
   ```bash
   kubectl exec headscale-0 -n tenant-test -c headscale -- headscale nodes list
   ```

## 3. Known pitfall: switching control servers / 已知陷阱：切換控制伺服器

If the add-on was **ever** logged in to the official Tailscale (or another server), it will crash-loop with:

```
can't change --login-server without --force-reauth
FATAL: Unable to start up Tailscale
```

The add-on does not expose `--force-reauth`. **Fix: uninstall → reinstall the add-on** (this clears the add-on's `/data` state), set `login_server` again, then start. This was verified in the live deployment — the HAOS appliance registered as node `woowtechshowha` (100.64.0.7) immediately after the clean reinstall.

## 4. Managing via HA MCP / 用 HA MCP 遠端管理

If you run an MCP server add-on on the appliance, the whole flow can be automated remotely:

| Step | MCP call |
|------|----------|
| Inspect add-on + options | `ha_get_addon(slug="a0d7b954_tailscale")` |
| Fix config | `ha_manage_addon(slug=…, options={login_server: …})` |
| Clear stale state | `ha_manage_addon(action="uninstall")` → `action="install"` |
| Start | `ha_manage_addon(action="start")` |
| Read auth URL from logs | `ha_get_logs(source="supervisor", slug="a0d7b954_tailscale")` |

Then complete registration with the Headscale CLI as in section 2.

## 5. Result / 成果

After registration the appliance is reachable from any tailnet device at its tailnet IP (e.g. `http://100.64.0.7:8123`), with the WireGuard data plane relayed via public DERP when NAT traversal fails.
