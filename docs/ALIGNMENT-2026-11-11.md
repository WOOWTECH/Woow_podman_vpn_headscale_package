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
| 公開 tunnel | ngrok HTTP／TCP，能讀動態 URL | ngrok HTTP，可用固定或動態 URL；公開路徑過濾 | 選用 ngrok HTTP，需固定 HTTPS 網域；私有路徑過濾只允許控制協定 |
| Prometheus metrics | 選用映射 | 私有端點 | 僅 loopback `127.0.0.1:29090` |
| 外部登入 | add-on 預設可設主機埠 | 同 workspace 或 ngrok | 目前僅區網 `192.168.2.46:28080`，防火牆僅放行 `192.168.2.0/24` |

HA ingress、Kubernetes StatefulSet／PVC、NetworkPolicy、tenant operator 和 JCR pull secret 是各平台的執行架構，Podman 以 Quadlet、Podman network／secret、UFW 及冷備份提供相應功能。

## 已執行驗收

1. `tests/dryrun.sh`：預設、選用 unit、LAN／proxy 及固定網域 ngrok fixture，共 5 種渲染，Podman 4.9.3 Quadlet generator 與 systemd-analyze 驗證通過。
2. `tests/lint-repo.sh`：憑證、版本與設定檔檢查通過。
3. `tests/smoke.sh`：25 passed、0 failed；未登入及錯誤密碼均為 401，正確 Podman secret 密碼開啟 Headplane（200）；公開路徑過濾對 `/health` 為 200、對 `/swagger` 為 404；Headscale CLI 和 metrics 正常。
4. `tests/real-client.py`：使用獨立 userspace `tailscaled` 和一次性 preauth key，經 `http://192.168.2.46:28080` 註冊；BackendState `Running`，取得 `100.64.0.1`／IPv6。測試節點及 key 事後清除，主機原有 tailnet 沒有切換。
5. 另一台 `192.168.2.121` 從區網對 Headscale `/health` 收到 200；此前 UFW 擋住，新增限定 `enp3s0`、`192.168.2.0/24` 到 `192.168.2.46:28080/tcp` 的規則後通過。
6. `scripts/backup.sh --include-secrets` 和 `scripts/restore.sh --confirm-restore headscale --restore-secrets` 實際執行；資料卷、API key、Cookie secret、入口密碼還原後三個單元啟動，還原後快速驗收 16 passed、0 failed。
7. 將原本十年有效期的 Headplane API key 換成一年金鑰，撤銷舊金鑰；換發後完整驗收 25 passed、0 failed，並再做一份含新金鑰的冷備份。
8. 兩個 Headplane secret 的容器內 0400 模式尚未由執行檔直接讀出：distroless Headplane 映像沒有 `stat`；Quadlet 指定 `mode=0400` 且 Podman 掛載檔唯讀。

## 尚未完成的情境

- 這台主機沒有 ngrok authtoken，故 tunnel 保持關閉；外網 TS2021 註冊與固定網域的實際連線尚未驗證。新增的選用 Quadlet 已通過靜態渲染，但不能把它列為已實測。
- Podman 選用 ngrok 目前只支援固定 HTTPS 網域。HA／PaaS 的動態 ngrok URL 發現及 HA 的 ngrok TCP 模式尚未搬入；若要對齊這兩種模式，還需規劃重啟時 `server_url` 更新及客戶端重新登入的處理。
- 管理入口目前只綁在 `127.0.0.1:23000`，可用 SSH tunnel。尚未設定公開 HTTPS 網域。沒有進行長時間運作、跨網路節點互連、DERP 中繼或多節點權限策略的實測。
- 目前驗收了 Headplane 頁面載入及授權流程，未逐頁操作所有網頁表單。

管理密碼由 `headscale-entrance-password` Podman secret 保存；報告與倉庫不包含明文金鑰。
