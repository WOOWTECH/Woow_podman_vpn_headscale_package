# Headscale Podman 與 HA add-on／PaaS 對照及實測

更新：2026-11-12（Asia/Taipei）

## 對照來源

- HA add-on：[`WOOWTECH/Woow_ha_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package)，main `43c2adf653b610d7d7005ed1d5d1bdb4b83cb2c1`，add-on 0.1.3 已發布並同步至 WOOW App Store。以 `headscale/config.yaml`、容器設定模板與服務啟動腳本為準；`192.168.2.121` 的正式實體已更新到 0.1.3。
- PaaS：cluster1 於 2026-11-12 經 Kubernetes API 的 Gitea Service proxy 實際讀取 `woow-paas-charts` 現行 main `7b42185d47dc6c860dd8390e733b764950e5df1e`。`charts/headscale` Git tree 為 `c119594183be93a3c489735091754259d5e582c4`，與封存 commit `573e6c471c8f4d2e84bdc80ba959c98f4aed5f08` 的同一路徑及全部 24 項 tree/blob SHA 相同；chart 仍為 0.1.8。以實際 Gitea commit 的 `values.yaml`、`templates/configmap.yaml`、`templates/entrance-configmap.yaml`、`templates/statefulset.yaml` 為準。
- Podman：`WOOWTECH/Woow_podman_vpn_headscale_package`，GitHub main `fc54bf19513c7d4b6ce51d8ae9a981cd0dda70a0`；PR #9–#12 均已合併。主機 `woowtechopenclaw` 的 rootless Podman 4.9.3、`systemd --user`。

## 功能對照

| 項目 | HA add-on | PaaS chart | 本次 Podman |
|---|---|---|---|
| 控制平面與網頁版本 | Headscale 0.29.4、Headplane 0.7.1；0.1.3 加 ACL SSR 修補 | 現行 chart 0.1.8、Headscale 0.29.4；live Headplane 帶 ACL SSR 修補，六次頁面請求均 200；平台 main 的後續 DNS／proxy-auth 修補尚未套用 live release | Headscale 0.29.4；Headplane 官方 0.7.1 基底加 ACL SSR、DNS 記錄與 proxy-auth 修補，來源提交及基底 digest 固定 |
| 控制平面資料 | `/data`，HA 備份 | SQLite + PVC、啟動遷移 | SQLite WAL + `headscale-data` volume、冷備份與還原 |
| DNS、節點期限、政策 | MagicDNS/base domain 與檔案政策；供應商、期限及 database mode 尚無 add-on 選項 | DNS 供應商、期限、資料庫政策 | MagicDNS、Cloudflare／Google／Quad9、期限、預設資料庫政策；另可選檔案政策及編輯獨立 DNS 記錄 |
| Headplane 入口 | HA ingress；現有 3650 天 API key 僅在檔案缺失時重建 | nginx Basic Auth + Headplane proxy_auth；API key 有效期 1 年、到期前 30 天換發 | 私有 Headplane + nginx Basic Auth + proxy_auth；健康檢查、headers、資產免限流；新 API key 有效期 1 年、到期前 30 天換發 |
| 公開 tunnel | ngrok HTTP／TCP，能讀動態 URL；目前 agent 直連 Headscale 8080 | ngrok HTTP，可用固定或動態 URL；公開路徑過濾 | ngrok HTTP 固定／dev 網域自動發現，以及測試用 TCP；兩者均經私有路徑過濾。正式服務仍用固定 HTTPS 網域 |
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

## PR #9／#10 後續補驗

1. ACL 500 已用 `tests/headplane-acl-ssr.py` 穩定重現：原始映像 5/5 回 500。直接繞過 nginx 從私有 Podman 網路請求也回 500；DNS 頁仍回 200。錯誤為 React SSR 的 `Element type is invalid ... undefined`。原始碼中 ACL 頁直接對 `.client` CodeMirror 元件做 `React.lazy()`；首次 SSR 後快取到伺服器端不存在的元件。`patches/headplane-acl-ssr.patch` 讓編輯器在瀏覽器 hydration 完成後才載入。釘選官方 v0.7.1 原始提交與官方基底映像，透過 `scripts/build-headplane.sh` 可重建。隔離容器連續 10 次直接請求 200；瀏覽器重載 3 次均載入 CodeMirror。正式部署前做無警告冷備份，切換後 `tests/smoke.sh` **30 passed、0 failed**，其中 ACL 連續 5 次 200。另用正式建置腳本重建測試映像並連續 5 次直接請求 200。
2. [GitHub 外部節點驗收](https://github.com/WOOWTECH/Woow_podman_vpn_headscale_package/actions/runs/43045428172)：GitHub 託管 runner 從區網外經固定 HTTPS ngrok 網域加入本機 Headscale，取得 `100.64.0.2`，本機一次性節點為 `100.64.0.1`。本機 → runner 及 runner → 本機的 TSMP 都成功；runner 的一般 `tailscale ping` 回報直接路徑。工作結束後 Headscale 節點為 0、無未使用且未過期的測試 key，GitHub 暫存 secret 與 variable 均已刪除。這證實跨網路節點互通；長時間、不同 ISP 的持續性仍未驗收。
3. HA add-on `1b7b4ce7_woow-headscale` 在 `192.168.2.121` 正運行且有 2 個節點，其 ngrok dev domain 供控制入口使用。2026-11-12 經允許短暫中斷後，完成下述無 `--url` agent 陽性驗收。[ngrok 官方文件](https://ngrok.com/docs/gateway/domains)說明每帳號只有一個自動分配且固定的 dev domain。PaaS Gitea 的現行 main 已於同日經 cluster1 叢集內 Service proxy 重讀。

## HA add-on ACL 修補發布

- HA 0.1.2 正式容器的 `/admin/acls` 連續五次回 500。相同的 hydration guard 已用於 HA 0.1.3；本機完整映像的隔離測試，在登入後連續五次取得實際 ACL 頁及 HTTP 200。
- [HA PR #15](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package/pull/15) 已合併；amd64、aarch64 建置和 [0.1.2→0.1.3 升級驗證](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package/actions/runs/43049953982) 均通過。升級驗證保留原 API key、使用者及 pre-auth key，並經 HA Ingress 登入後對 ACL 頁連續五次確認 200。`v0.1.3` Release 與 WOOW App Store 同步完成。
- 正式 HA 主機更新前建立部分備份 `546add48`，確認包含 Headscale add-on；更新到 0.1.3 後 `started`、LAN 和 ngrok 公開 `/health` 均為 200，原有 2 個節點保留。正式容器的 Headplane API key 登入得到 302，ACL 頁連續五次回 200 且含實際 ACL 內容；dev domain 驗收後重新啟動，再跑同一測試仍通過。

## 2026-11-12 ngrok dev domain 真實驗收

- 暫停 HA add-on 後，將其帳號授權暫存於本機 Podman secret，啟動臨時 ngrok agent；啟動參數僅有 `http headscale-entrance:8081 --log stdout --inspect=false`，沒有 `--url`。agent API 實際分配 `https://loving-woodcock-cleanly.ngrok-free.app`；公開 `/health` 為 200，私有 `/admin/` 為 404。
- 用與正式 `scripts/ngrok-sync.py` 相同的 URL 解析與三檔同步函式，將真實分配的網址寫入隔離的 `headscale.env`、Headscale 設定和 Headplane 設定副本；三者值一致，第二次同步無變更。正式 Podman 固定網域和設定未切換。此測試證實真實 dev domain 的分配及設定同步；沒有在 dev domain 上重新註冊 Tailscale 用戶端。
- 移除臨時 agent 和 Podman secret 後重新啟動 HA add-on：0.1.3 `started`、原有節點數仍為 2，HA 與 Podman 的公開 `/health` 均為 200，Podman 四個 Quadlet unit 均 active。

## 2026-11-12 PaaS 現行來源與現場補驗

- 由 Omnigent 在 `woowtechcluster1-AORUS-15P-XD` 建立 Codex 對話，經 Tailscale 上的 Kubernetes API、叢集內 `gitea/gitea-http:3000` Service proxy 讀取 Gitea。保留 kubeconfig 既有 CA、client certificate 和 TLS server name 驗證；無需經 Cloudflare Access 公開入口。現行 `woow-paas-charts` main 為 `7b42185d47dc6c860dd8390e733b764950e5df1e`；其 `charts/headscale` tree `c119594183be93a3c489735091754259d5e582c4` 與封存 `573e6c4` 完全相同。這項結論來自 Gitea 的 tree/blob SHA 比對，不是經刪敏的檔案副本；副本不能拿來渲染或部署。
- PaaS 正式 tenant release `svc-2ed8c6f8`，namespace `paas-ws-53c0990e`：StatefulSet 的 `helm.sh/chart=headscale-0.1.8`、`app.kubernetes.io/version=v0.29.4`，1/1 Ready。Headscale 的 JCR 映像 digest 為 `sha256:8833f828b414c0907b7e5c71da76473216fe17cce0818a166b536ec552c0903f`；Headplane 正運行 `20261013.1924@sha256:6ba6c42bb9e277c729ae69c6d42c4ba367655e7b4e8b03a22189204ecfb4a4c5`。版本與 live imageID 從 K8s 讀取，不以 chart 預設映像推定。
- 透過現行 Pod 的 loopback 端點讀 `/admin/acls` 六次，全部 HTTP 200，頁面有 HTML 且沒有 SSR 錯誤；`/admin/dns` 為 200，未設定 OIDC 的 `/admin/settings/restrictions` 為 501，`/admin/agent` 為 404。供 ngrok 使用的 loopback path filter 對 `/health` 回 200、對 `/version`、`/swagger`、`/api/v1/policy` 回 404；入口未認證的 `/admin/acls` 為 401。這是服務端 HTTP 驗收，不是公網 ngrok 端到端或登入後 ACL 寫入驗收。
- PaaS 現場 config template 為 database policy；Podman 預設也是 database，可選 file；HA add-on 0.1.3 預設 `policy.mode: file`、`policy.path: ""`。HA 能以設定目錄提供 HuJSON 政策，但預設並非 Headplane 資料庫政策編輯流程。PaaS chart 僅提供 ngrok HTTP；HA add-on 和 Podman 有 HTTP／TCP。PaaS 現場 nginx 的 `/admin/assets/` 仍計入 `limit_req`；Podman 已豁免資產請求，避免載入網頁時的 429。這些是現行設定／原始碼差異，不能以健康檢查 200 推論為全部操作等價。
- 同日重查：HAOS `192.168.2.121:28080/health` 200、`/version` 回報 v0.29.4；Podman 的區網與 ngrok `/health` 均 200，`headscale`、`headplane`、`headscale-entrance`、`headscale-ngrok` 四個 user unit active。HA add-on GitHub main 仍為 `43c2adf653b610d7d7005ed1d5d1bdb4b83cb2c1`，已包含 ACL SSR 修補。
- 平台 `woow_paas_platform` main `c532c106f6d8739ef64359826adaf136f0b620ec` 已 pin Headplane `20261104.1327@sha256:1acc2065a56d2edb41b9ab22fca504f6532f8ee79ff79f4a307ada9c8c7a2841`，來源為 `WOOWTECH/headplane` 的 `b2780ebabea3110df307be4302ae910f4c5dd2df`；現場仍跑 `a02030c7` 所建的舊 digest。`b2780eb` 修正主設定唯讀時額外 DNS 記錄的編輯判斷，及 proxy-auth 管理員的帳號連結選擇頁。live Headplane 目前的 ACL 6/6 200 由舊映像已含的 SSR patch 達成。
- HA add-on 0.1.3 的 ngrok agent 直連 8080，沒有 PaaS／Podman 的公開控制路徑 allowlist；現有 API key 檔若非空只沿用，首次建檔有效期 3650 天，沒有失效或剩餘 30 天輪替判斷。這是來源層的能力差異，不代表本輪對 HA 正式實體執行了路徑攻防或輪替測試。

## 2026-11-12 Podman Headplane 後續修補驗收

- 從 `WOOWTECH/headplane` `b2780eb` 精確取出五個執行期檔案的 DNS／proxy-auth 修改，套在固定官方 0.7.1 原始碼與現有 ACL SSR patch 上。保留官方基底 digest，建出 `localhost/woow-headplane:0.7.1-woow2`。fork 所附的兩組單元測試以非 root 執行，10/10 通過。
- 新舊映像各在隔離容器、隔離資料目錄執行：新映像 `/admin/acls`、`/admin/dns` 連續 200；新映像 DNS 表單的新增及刪除均寫入隔離 extra-records 檔，主設定 SHA-256 不變。舊映像的表單請求導向 Machines，未寫入記錄；proxy-auth 首頁由舊映像的 200 選擇頁改為新映像 302 導向 `/admin/machines`。
- 正式切換前 `scripts/upgrade.sh` 完成含 secret 的一致性冷備份，更新後 `tests/smoke.sh` 30 passed、0 failed、0 warnings；ACL 連續五次 200。以正式 Basic Auth 入口新增並刪除唯一測試 DNS 記錄，核對原有記錄完全恢復、主設定檔雜湊不變。正式入口 `/admin/` 為 302 導向 Machines；公開 ngrok `/health` 200、`/admin/` 404，四個 unit active。舊 `0.7.1-ssr1` 映像仍在本機供回退。

## 仍待驗收

- PaaS 目前平台 `main` 指定的 Headplane 映像與既有 tenant release 的映像 digest 不同；已確認現行 release 的 ACL 頁連續六次 200，尚未把平台新預設映像套用到這個 release 驗收。
- PaaS 的 ACL 新增／儲存、完整 Headplane 瀏覽器操作，以及 ngrok 連接下的真實外網 Tailscale 節點互通未於本輪操作；PaaS 資產限流差異由現場 nginx 設定確認，未以多人瀏覽器負載重現 429。
- 管理入口仍只綁 `127.0.0.1:23000`，可用 SSH tunnel。已完成 GitHub 雲端與本機的跨網路互通及強制 DERP 測試；未做長時間連線、多節點權限、真實 OIDC 或 Headplane Agent 情境。
- DNS 主設定的網頁編輯因 Headscale 設定唯讀而禁用；獨立額外 DNS 記錄已在 Podman 正式入口完成新增／刪除驗收。PaaS live 仍是較舊 Headplane 映像，主設定唯讀時的獨立 DNS 記錄寫入未驗；HA 的登入後 DNS／ACL 操作本輪未測。PaaS 現場資產請求仍計入限流，429 尚未以瀏覽器負載重現。

管理密碼由 `headscale-entrance-password` Podman secret 保存；報告與倉庫不包含明文金鑰。
