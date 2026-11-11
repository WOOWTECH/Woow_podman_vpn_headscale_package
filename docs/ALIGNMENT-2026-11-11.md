# Headscale Podman 與 HA add-on／PaaS 對照及實測

更新：2026-11-11（Asia/Taipei）

## 對照來源

- HA add-on：[`WOOWTECH/Woow_ha_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package)，main `1d774f077ebb6172accfb9ab927522ba45b9236d`，add-on 0.1.2。以 `headscale/config.yaml`、容器設定模板與服務啟動腳本為準。
- PaaS：Gitea `woow-paas-charts/charts/headscale`，main `573e6c471c8f4d2e84bdc80ba959c98f4aed5f08`，chart 0.1.8。透過 cluster1 的 kubeconfig 與 Gitea service 讀取並封存當前 main；以 `values.yaml`、`templates/configmap.yaml`、`templates/entrance-configmap.yaml`、`templates/statefulset.yaml` 為準。
- Podman：`WOOWTECH/Woow_podman_vpn_headscale_package`；PR #9 已合併，後續 ACL 修補與外網驗收在 `feat/headscale-complete-parity-20261111`。主機 `woowtechopenclaw` 的 rootless Podman 4.9.3、`systemd --user`。HA main 於本輪重新 fetch，仍為 `1d774f0`；PaaS 以當時封存的 `573e6c4` 對照，直接讀 Gitea main 得到 Cloudflare Access 302，未重新取得現行內容。

## 功能對照

| 項目 | HA add-on | PaaS chart | 本次 Podman |
|---|---|---|---|
| 控制平面與網頁版本 | Headscale 0.29.4、Headplane 0.7.1 | 同左 | Headscale 0.29.4；Headplane 官方 0.7.1 基底加 ACL SSR 修補，原始提交與基底 digest 固定 |
| 控制平面資料 | `/data`，HA 備份 | SQLite + PVC、啟動遷移 | SQLite WAL + `headscale-data` volume、冷備份與還原 |
| DNS、節點期限、政策 | MagicDNS、base domain、檔案政策 | DNS 供應商、期限、資料庫政策 | MagicDNS、Cloudflare／Google／Quad9、期限、預設資料庫政策；另可選檔案政策 |
| Headplane 入口 | HA ingress | nginx Basic Auth + Headplane proxy_auth；API key 有效期 1 年、到期前 30 天換發 | 私有 Headplane + nginx Basic Auth + proxy_auth；健康檢查、headers、每來源速率上限；新 API key 有效期 1 年、到期前 30 天換發 |
| 公開 tunnel | ngrok HTTP／TCP，能讀動態 URL | ngrok HTTP，可用固定或動態 URL；公開路徑過濾 | ngrok HTTP 固定／dev 網域自動發現，以及測試用 TCP；兩者均經私有路徑過濾。正式服務仍用固定 HTTPS 網域 |
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

## 本輪補測與修正

1. `tests/dryrun.sh`：固定 HTTPS、自動 HTTP、TCP 均用 Podman 4.9.3 Quadlet generator 與 systemd 驗證；合計 7 種渲染、0 失敗。`tests/ngrok-sync-test.py` 驗證 URL 格式、防止多個候選 URL、TCP `tcp://` 轉 Headscale `http://`、三份設定同步與重跑無變更。
2. 正式固定 HTTPS agent 的本機 API 回報 `https://woow-hs-podman-46.ngrok.app`；`scripts/ngrok-sync.py` 在隔離設定副本上，以空白網域自動發現相同網址，且正確同步 `headscale.env`、Headscale、Headplane 三份設定。正式 Quadlet 重啟後 `tests/smoke.sh` 為 29 passed、0 failed，agent 實際網址與 `HEADSCALE_SERVER_URL` 一致。停止 nginx 入口時重新啟動 agent，確認它能先註冊 tunnel；恢復 nginx 後公開 `/health` 再度 200。
3. `tests/ngrok-tcp-uat.py`：從 ngrok API 產生短期 agent credential，僅暫停本機 HTTPS tunnel；臨時 TCP endpoint `tcp://0.tcp.jp.ngrok.io:11175` 的 `/health` 為 200。將本機 `server_url` 短暫改為 `http://0.tcp.jp.ngrok.io:11175`，兩個 Tailscale userspace client 經 TCP 註冊，取得 IPv4/IPv6，WireGuard TSMP ping 成功。之後刪除測試節點、作廢預授權 key、移除臨時容器與 secret、撤銷臨時 agent credential，恢復固定 HTTPS；四個 unit active、公開 `/health` 為 200。TCP 模式仍只供無 TLS 的短期測試，正式未切換。
4. `tests/remote-peers.py`：本機與 `192.168.2.121` 各以一次性 key 註冊一個節點，雙向 TSMP ping 成功；強制 DERP 時 `tailscale ping` 明確回報 `via DERP(tok)`，雙向 TSMP 仍成功。兩端在同一區網，但控制連線走公開 ngrok URL，DERP 測試強制走中繼；這不能替代兩個不同 ISP 的長時間實測。遠端臨時檔、節點與 key 已清理。
5. Headplane 瀏覽器驗證：首頁、Machines、Users、DNS、ACL、Settings、Pre-Auth Keys、Agent 頁可載入；以 UI 建立並刪除暫存使用者、建立並作廢 pre-auth key、有在線節點時開啟 Machine 詳細頁並改名，結果均在 Headscale CLI 核對。入口原來會把大量靜態模組計入每秒 5 次限流而回 429；已讓 `/admin/assets/` 不計入限流，重跑各頁未再遇到 429，管理頁與密碼驗證仍受限流／Basic Auth 保護。
6. 原始 Headplane 0.7.1 的 ACL 頁在第一次 200 後反覆回 500，即使有效資料庫 policy 存在仍會發生；暫存 policy 已刪除，原本空政策狀態恢復。Restrictions 頁沒有 OIDC 設定，依上游程式碼回 501；Agent 未啟用時顯示設定說明。DNS 表單因 Headscale 設定唯讀被 Headplane 禁用。

## PR #9 後續補驗

1. ACL 500 已用 `tests/headplane-acl-ssr.py` 穩定重現：原始映像 5/5 回 500。直接繞過 nginx 從私有 Podman 網路請求也回 500；DNS 頁仍回 200。錯誤為 React SSR 的 `Element type is invalid ... undefined`。原始碼中 ACL 頁直接對 `.client` CodeMirror 元件做 `React.lazy()`；首次 SSR 後快取到伺服器端不存在的元件。`patches/headplane-acl-ssr.patch` 讓編輯器在瀏覽器 hydration 完成後才載入。釘選官方 v0.7.1 原始提交與官方基底映像，透過 `scripts/build-headplane.sh` 可重建。隔離容器連續 10 次直接請求 200；瀏覽器重載 3 次均載入 CodeMirror。正式部署前做無警告冷備份，切換後 `tests/smoke.sh` **30 passed、0 failed**，其中 ACL 連續 5 次 200。另用正式建置腳本重建測試映像並連續 5 次直接請求 200。
2. [GitHub 外部節點驗收](https://github.com/WOOWTECH/Woow_podman_vpn_headscale_package/actions/runs/43045428172)：GitHub 託管 runner 從區網外經固定 HTTPS ngrok 網域加入本機 Headscale，取得 `100.64.0.2`，本機一次性節點為 `100.64.0.1`。本機 → runner 及 runner → 本機的 TSMP 都成功；runner 的一般 `tailscale ping` 回報直接路徑。工作結束後 Headscale 節點為 0、無未使用且未過期的測試 key，GitHub 暫存 secret 與 variable 均已刪除。這證實跨網路節點互通；長時間、不同 ISP 的持續性仍未驗收。
3. HA add-on `1b7b4ce7_woow-headscale` 在 `192.168.2.121` 正運行且有 2 個節點，配置的 ngrok dev domain 正供它使用。未中斷其控制入口；等待短時中斷授權或另一帳號後，才能直接啟動 Podman 無 `--url` agent 做陽性驗收。[ngrok 官方文件](https://ngrok.com/docs/gateway/domains)說明每帳號只有一個自動分配且固定的 dev domain。PaaS 現行 Gitea main 仍被 Cloudflare Access 擋住；已請求 cluster1 kubeconfig／SSH 或 Access 授權。

## 仍待驗收

- 本機 ngrok 帳號的 dev domain 正由已有兩個節點的 HA add-on 使用；自動發現解析與寫入已對真實 agent 做隔離實測，但 Podman 無 `--url` agent 的實際分配仍待可用網域。PaaS Gitea main 也未在本輪重新讀取，對齊結論以封存的 chart 0.1.8 為限。
- 管理入口仍只綁 `127.0.0.1:23000`，可用 SSH tunnel。已完成 GitHub 雲端與本機的跨網路互通及強制 DERP 測試；未做長時間連線、多節點權限、真實 OIDC 或 Headplane Agent 情境。
- DNS 主設定的網頁編輯因 Headscale 設定唯讀而禁用；PaaS chart 同樣以唯讀 ConfigMap 掛載 Headplane 設定，可由 chart 值修改。PaaS／HA 的實際網頁狀態仍需登入各自環境確認。PaaS chart 的入口限流也會計算 `/admin/assets/`，可能遇到相同 429，這是由 chart 設定推論，尚未在 PaaS 瀏覽器實測。

管理密碼由 `headscale-entrance-password` Podman secret 保存；報告與倉庫不包含明文金鑰。
