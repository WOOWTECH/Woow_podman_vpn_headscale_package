# Headscale + Headplane 自架 VPN — 部署報告

> **Historical.** This report records the compose-era deployment, kept at the `compose-final` tag.
> The supported deployment is now Quadlet + systemd; see the README.
>
> **歷史文件。** 本報告記錄 compose 時代的部署（保留於 `compose-final` tag）。目前支援的部署方式為 Quadlet + systemd，請見 README。


> 部署時間：2026-07-16
> 叢集：WoowTech K3s（10 nodes, v1.34.x）

---

## 已完成的 Phase

### Phase 1 — headscale-operator ✅
- **Chart**: `oci://ghcr.io/infradohq/headscale-operator/charts/headscale-operator:0.5.0`
- **Namespace**: `headscale-system`
- **CRDs**: 4 個已註冊（Headscale, HeadscaleUser, HeadscalePreAuthKey, HeadscaleAutoApprover）
- **Pod**: `headscale-operator-controller-manager` Running

### Phase 2 — 測試租戶 Headscale ✅
- **Namespace**: `tenant-test`
- **Headscale**: v0.29.1, StatefulSet 1/1, PVC on `nfs-data`
- **Server URL**: `https://vpn-test.woowtech.io`
- **MagicDNS base domain**: `ts.woowtech.io`（不可與 server_url 同域名）
- **gRPC**: port 50443, `grpc_allow_insecure: true`（operator 需要此設定）
- **API Key**: 自動管理，Secret `headscale-api-key`
- **User**: `default` (HeadscaleUser CR)

### Phase 3 — Headplane ✅
- **版本**: v0.7.0 (`ghcr.io/tale/headplane:0.7.0`)
- **Config**: 不能包含 `integration.kubernetes` 除非提供 `pod_name`，移除後正常
- **連接**: 成功連到 Headscale 0.29.1
- **Service**: `headplane.tenant-test.svc.cluster.local:3000`

### Phase 3.5 — Cloudflare Tunnel ✅（部分）
- **Tunnel routes 已設定**:
  - `vpn-test.woowtech.io` → `http://headscale.tenant-test.svc.cluster.local:8080`
  - `admin-test.woowtech.io` → `http://headplane.tenant-test.svc.cluster.local:3000`
- **⚠️ 待辦**: 需在 Cloudflare Dashboard 新增 DNS CNAME records

### Phase 4 — PreAuthKey ✅
- **Key**: `hskey-auth-…`（已撤銷，僅為歷史紀錄）（儲存在 Secret `test-device-preauth-key`）
- **設定**: reusable, 72h 到期
- **連線指令**: `tailscale up --login-server=https://vpn-test.woowtech.io --authkey=<key>`
- **⚠️ 待 DNS 設定後才能實測外部連線**

### Phase 5 — 自動路由核准 ✅（用 inline policy 替代 CRD）
- **問題**: HeadscaleAutoApprover CRD 的 `tag_owners` 與 Headscale v0.29 的 v2 policy parser 格式不相容
- **解法**: 在 Headscale CR 的 `acl_policy.inline` 直接定義 `autoApprovers`
- **已設定**: `10.0.0.0/8` 和 `192.168.0.0/16` 自動核准，exit node 自動核准

### Phase 6 — 服務接入 VPN ✅（部署完成，待 DNS 驗證）
- **test-nginx**: Deployment + ClusterIP Service (port 80)
- **tailscale-proxy-nginx**:
  - Image: `tailscale/tailscale:latest`
  - `TS_DEST_IP` → nginx ClusterIP
  - `TS_USERSPACE=false` + `NET_ADMIN` capability + `/dev/net/tun`
  - ServiceAccount + RBAC（secrets get/create/update/patch）
  - 正在重試連接（等待 DNS）

### Phase 7 — Litestream 備份 ⏸️ 延後
- **原因**: 叢集無 S3 相容物件儲存（MinIO 等）
- **建議**: 未來部署 MinIO 後再加入 Litestream sidecar

---

## 發現的問題與解法

| 問題 | 原因 | 解法 |
|------|------|------|
| `server_url` 與 `base_domain` 衝突 | Headscale 不允許 DERP 和 server 共用域名 | `base_domain` 改用不同域名 `ts.woowtech.io` |
| gRPC port 50443 連不上 | Headscale v0.29 預設不啟動 gRPC | 設定 `grpc_allow_insecure: true` |
| HeadscaleUser 建立失敗 | 建立時 Headscale pod 還沒 ready | Operator 自動重試成功（需等 gRPC 開通） |
| Headplane `pod_name` 必填 | v0.7.0 即使 `enabled: false` 也驗證 | 移除整個 `integration` 區段 |
| AutoApprover tag_owners 格式不相容 | Headscale v0.29 policy v2 format 要求不同 | 用 `acl_policy.inline` 的 `autoApprovers` 替代 |
| tailscale proxy TS_DEST_IP 不支援 userspace | 預設 `TS_USERSPACE=true` | 明確設 `TS_USERSPACE=false` |
| tailscale proxy 缺 RBAC | K8s 模式需要 secrets 權限 | 建立 ServiceAccount + Role + RoleBinding |
| tailscale proxy 無 tun device | 非 userspace 模式需要 `/dev/net/tun` | 掛載 hostPath `/dev/net/tun` |
| **Cloudflare Tunnel 不支援 Tailscale noise protocol** | CF 會剝離非標準 HTTP Upgrade headers（`Upgrade: tailscale-control-protocol`） | 叢集內 proxy pod 用內部 URL `http://headscale...svc:8080`；外部裝置需 NodePort 或非 proxied DNS |
| CoreDNS 快取 NXDOMAIN 30 分鐘 | SOA minimum TTL=1800s | 加 CoreDNS custom forward 直接用 1.1.1.1 解析 `woowtech.io` |
| Cloudflare Tunnel 用遠端管理 | ConfigMap 無效，tunnel 實際讀遠端 config | 用 Cloudflare API (`PUT /cfd_tunnel/{id}/configurations`) 更新 routes |

---

## 已驗證結果

| 測試項目 | 結果 |
|---------|------|
| Headscale `/health` (外部) | ✅ HTTP 200 `{"status":"pass"}` |
| Headplane `/admin` (外部) | ✅ HTTP 302 (redirect to login) |
| Tailscale proxy pod 連線 | ✅ 用內部 URL 成功連上，DERP relay connected |
| nginx-test 節點上線 | ✅ IP 100.64.0.1, status=online, ephemeral |

---

## 待辦事項

1. **[重要]** 外部裝置連線需要不經 Cloudflare proxy 的曝露方式：
   - 方案 A：NodePort Service 暴露 Headscale（需 firewall 開放）
   - 方案 B：Cloudflare DNS 設為 DNS-only（proxied=false）+ 直接 TLS
   - 方案 C：在有公網 IP 的節點用 Traefik IngressRoute 直接曝露
   - 原因：Cloudflare 會剝離 Tailscale noise protocol 的非標準 HTTP Upgrade header

3. **[未來]** Litestream 備份：部署 MinIO → 加 Litestream sidecar
4. **[未來]** OIDC 串接 OpenClaw 登入系統
5. **[未來]** 多租戶自動化模板（Helm chart 或模板化 YAML）

---

## 元件版本與 Image

| 元件 | 版本 | Image |
|------|------|-------|
| headscale-operator | chart 0.5.0 (app v0.6.0) | `ghcr.io/infradohq/headscale-operator` |
| Headscale | v0.29.1 | `headscale/headscale:v0.29.1` |
| Headplane | v0.7.0 | `ghcr.io/tale/headplane:0.7.0` |
| Tailscale Proxy | latest | `tailscale/tailscale:latest` |

---

## 檔案清單

```
headscale/
├── DEPLOYMENT-REPORT.md                           # 本報告
└── tenant-test/
    ├── 01-namespace.yaml                          # Namespace
    ├── 02-headscale-cr.yaml                       # Headscale CRD (含 ACL inline policy)
    ├── 03-headscale-user.yaml                     # HeadscaleUser: default
    ├── 05-headplane-secret.yaml                   # Headplane cookie secret (template)
    ├── 06-headplane-configmap.yaml                # Headplane config
    ├── 07-headplane-deployment.yaml               # Headplane Deployment + Service
    ├── 08-cloudflared-config-patch.yaml           # Cloudflare Tunnel config (含新 routes)
    ├── 09-preauth-key.yaml                        # HeadscalePreAuthKey: test device
    ├── 10-auto-approver.yaml                      # AutoApprover (已註解, 用 inline 替代)
    ├── 11-test-nginx.yaml                         # Test nginx Deployment + Service
    ├── 12-proxy-preauth-key.yaml                  # HeadscalePreAuthKey: proxy (ephemeral)
    └── 13-tailscale-proxy.yaml                    # Tailscale proxy Deployment + RBAC
```
