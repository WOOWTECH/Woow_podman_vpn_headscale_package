# Headscale Podman 與 HA add-on／PaaS 對照及實測

更新：2026-11-11（Asia/Taipei）

## 對照來源

- HA add-on：[`WOOWTECH/Woow_ha_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package)，main `1d774f077ebb6172accfb9ab927522ba45b9236d`，add-on 0.1.2。以 `headscale/config.yaml`、容器設定模板與服務啟動腳本為準。
- PaaS：Gitea `woow-paas-charts/charts/headscale`，main `573e6c471c8f4d2e84bdc80ba959c98f4aed5f08`，chart 0.1.8。透過 cluster1 的 kubeconfig 與 Gitea service 讀取並封存當前 main；以 `values.yaml`、`templates/configmap.yaml`、`templates/entrance-configmap.yaml`、`templates/statefulset.yaml` 為準。
- Podman：`WOOWTECH/Woow_podman_vpn_headscale_package`，本次分支 `feat/align-headscale-ha-paas-20261111`。主機 `woowtechopenclaw` 的 rootless Podman 4.9.3、`systemd --user`。

## 功能對照

| 項目 | HA add-on | PaaS chart | 本次 Podman |
|---|---|---|---|
| 控制平面與網頁版本 | Headscale 0.29.4、Headplane 0.7.1 | 同左 | 同左，映像版本及 digest 固定 |
| 控制平面資料 | `/data`，HA 備份 | SQLite + PVC、啟動遷移 | SQLite WAL + `headscale-data` volume、冷備份與還原 |
| DNS、節點期限、政策 | MagicDNS、base domain、檔案政策 | DNS 供應商、期限、資料庫政策 | MagicDNS、Cloudflare／Google／Quad9、期限、預設資料庫政策；另可選檔案政策 |
| Headplane 入口 | HA ingress | nginx Basic Auth + Headplane proxy_auth；API key 有效期 1 年、到期前 30 天換發 | 私有 Headplane + nginx Basic Auth + proxy_auth；健康檢查、headers、每來源速率上限；新 API key 有效期 1 年、到期前 30 天換發 |
| 公開 tunnel | ngrok HTTP／TCP，能讀動態 URL | ngrok HTTP，可用固定或動態 URL；公開路徑過濾 | ngrok HTTP，明確指定固定 HTTPS 網域；私有路徑過濾只允許控制協定，已實際上線與註冊 |
| Prometheus metrics | 選用映射 | 私有端點 | 僅 loopback `127.0.0.1:29090` |
| 外部登入 | add-on 預設可設主機埠 | 同 workspace 或 ngrok | 區網 `192.168.2.46:28080` 限 `192.168.2.0/24`；外網控制協定由 `https://woow-hs-podman-46.ngrok.app` 進入 |

HA ingress、Kubernetes StatefulSet／PVC、NetworkPolicy、tenant operator 和 JCR pull secret 是各平台的執行架構，Podman 以 Quadlet、Podman network／secret、UFW 及冷備份提供相應功能。

## 已執行驗收

1. `tests/dryrun.sh`：預設、選用 unit、LAN／proxy 及固定網域 ngrok fixture，共 5 種渲染，Podman 4.9.3 Quadlet generator 與 systemd-analyze 驗證通過。
2. `tests/lint-repo.sh`：憑證、版本與設定檔檢查通過。
3. `tests/smoke.sh`：開啟 ngrok 後 28 passed、0 failed；未登入及錯誤密碼均為 401，正確 Podman secret 密碼開啟 Headplane（200）；私有路徑過濾對 `/health` 為 200、對 `/swagger` 為 404；Headscale CLI 和 metrics 正常。
4. `tests/real-client.py`：使用獨立 userspace `tailscaled` 和一次性 preauth key，經 `http://192.168.2.46:28080` 註冊；BackendState `Running`，取得 `100.64.0.1`／IPv6。測試節點及 key 事後清除，主機原有 tailnet 沒有切換。
5. 另一台 `192.168.2.121` 從區網對 Headscale `/health` 收到 200；此前 UFW 擋住，新增限定 `enp3s0`、`192.168.2.0/24` 到 `192.168.2.46:28080/tcp` 的規則後通過。
6. `scripts/backup.sh --include-secrets` 和 `scripts/restore.sh --confirm-restore headscale --restore-secrets` 實際執行；資料卷、API key、Cookie secret、入口密碼還原後三個單元啟動，還原後快速驗收 16 passed、0 failed。
7. 將原本十年有效期的 Headplane API key 換成一年金鑰，撤銷舊金鑰；換發後完整驗收通過，並再做一份含新金鑰的冷備份。
8. 以 Headplane 映像內建的 Node.js `fs.statSync` 驗證兩個 secret 的容器內權限均為 0400。
9. ngrok 平台 API key 驗證成功；確認原有兩個網域均在使用，另保留 `woow-hs-podman-46.ngrok.app`，由 API 建立只允許 `bind:woow-hs-podman-46.ngrok.app` 的 agent authtoken。agent token 保存在 `headscale-ngrok-authtoken` Podman secret；提供的 API key 在完成查詢及建置後已從本機暫存 secret 移除。原有兩個 endpoint 維持在線。
10. 公開 HTTPS `GET /health` 得到 200；`/swagger`、`/api/v1`、`/admin/`、`/version`、`/debug/pprof` 和 `//admin/` 為 404，編碼跳轉 `/%2e%2e/admin/` 為 400。另一台 `192.168.2.121` 經公開網域取得 `/health` 200、TLS 驗證結果 0，管理及 API 路徑 404。
11. `tests/real-client.py` 經公開 ngrok 網域註冊成功，BackendState `Running`，取得 IPv4 與 IPv6。`tests/real-peers.py --restart-tunnel` 讓兩個獨立 userspace client 經 ngrok 註冊，彼此的 WireGuard TSMP ping 在隧道重啟前後均成功；測試節點及一次性預授權 key 事後清除，現有節點數為 0。
12. 開啟 ngrok 後再執行 `scripts/backup.sh --include-secrets`：四個 user unit 全部停下完成一致性備份，重啟後公開 `/health` 和真實 client 註冊再次通過。備份與還原腳本加入 user systemd 可連線檢查，避免缺少 `XDG_RUNTIME_DIR` 時跳過停機，錯把在線資料卷當作冷備份。

## 尚未完成的情境

- Podman ngrok 仍要求事先填入固定 HTTPS 網域；HA／PaaS 的自動 URL 發現及 HA 的 ngrok TCP 模式尚未搬入。ngrok 現行的帳號 dev domain 本身可跨重啟保持不變，但此帳號的 dev domain 已供另一個服務使用，因此本機使用獨立保留網域。
- 管理入口仍只綁在 `127.0.0.1:23000`，可用 SSH tunnel。尚未設定公開 HTTPS 管理入口。已驗證兩個本機隔離 client 的 WireGuard 互通，但未進行長時間運作、兩個不同外網節點互連、DERP 路徑或多節點權限策略的實測。
- 目前驗收了 Headplane 頁面載入及授權流程，未逐頁操作所有網頁表單。

管理密碼由 `headscale-entrance-password` Podman secret 保存；報告與倉庫不包含明文金鑰。
