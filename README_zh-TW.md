# Headscale + Headplane on rootless Podman（Quadlet + systemd）

在單一主機上自架 Tailscale 控制平面：**Headscale 0.29.3** 提供 VPN，**Headplane 0.7.0** 作為管理
介面，兩者都以 rootless Quadlet unit 安裝，由 `systemd --user` 管理。官方 Tailscale 客戶端不需修改
即可註冊。

English: [README.md](README.md)

> **Docker / podman-compose 使用者：** compose 部署已不在 `main`。它原封不動保留在
> **[`compose-final`](../../tree/compose-final)** tag：`git checkout compose-final`。

---

## 安裝內容

| Unit | 容器 | 映像 | 預設發布位址 |
|---|---|---|---|
| `headscale.service` | `headscale` | `localhost/woow-headscale:0.29.3-r1`（本機建置） | `127.0.0.1:28080` → 8080、`127.0.0.1:29090` → 9090 |
| `headplane.service` | `headplane` | `ghcr.io/tale/headplane:0.7.0`（digest 釘選） | `127.0.0.1:23000` → 3000 |
| `headscale-network.service` | – | – | podman 網路 `headscale-net` |
| `headscale-data-volume.service` | – | – | volume `headscale-data`（SQLite 與 noise key） |
| `headscale-run-volume.service` | – | – | volume `headscale-run`（gRPC unix socket） |
| `headplane-data-volume.service` | – | – | volume `headplane-data` |

**兩者為何分開。** Headscale 就是 VPN 本身：節點資料庫、noise key 與政策都在它手上，客戶端也只跟它
通訊。Headplane 只是 Headscale REST API 前面的網頁介面，把它停掉不會有任何節點受影響。分成兩個
unit 的理由：

* Headplane 可以單獨重啟、升級或移除，完全不動到控制平面；
* Headplane 透過 podman 網路以容器名稱連到 Headscale（`http://headscale:8080`），因此 API 不必為了
  它而發布到主機；
* Headplane 用的 API key 只能由**已啟動**的 Headscale 產生，所以安裝必須先啟動一個 unit、建立 key，
  再啟動另一個。這個順序寫死在 `scripts/install.sh`，以及 `headplane.container` 的
  `Requires=`／`After=` 加上開機時等待 `headscale health` 的 `ExecStartPre=` 迴圈。

## 需求

* Ubuntu 24.04（或任何具備 **podman ≥ 4.9** 與 systemd 255 的系統），rootless，並啟用 linger
* `git`、`curl`、`python3`，首次安裝需要對外網路（會建置映像）
* 全程不使用 root：請以將擁有這些容器的帳號執行所有腳本，絕不加 `sudo`

## 安裝

```bash
git clone https://github.com/WOOWTECH/Woow_podman_vpn_headscale_package.git
cd Woow_podman_vpn_headscale_package
scripts/install.sh                 # 第一次執行會寫出設定檔後停下來
$EDITOR ~/.config/headscale/headscale.env
scripts/install.sh                 # 建置、安裝、啟動、煙霧測試
```

第一次執行會以 `config/headscale.env.example` 建立 `~/.config/headscale/headscale.env`（權限
0600）並停下來讓你檢查設定；加 `--accept-defaults` 可略過。`scripts/install.sh` 具備冪等性：內容
沒變的重跑不會建置、不會重啟，也會保留兩個 secret。

常用旗標：`--set KEY=VALUE`、`--dry-run`（只渲染、驗證並報告，不動任何東西）、`--rebuild`、
`--no-build`、`--build-only`、`--rotate-api-key`、`--no-start`、`--no-smoke`。

### 設定

所有 per-host 設定都在 `~/.config/headscale/headscale.env` — `KEY=VALUE`、不加引號，而且
**值後面不可以有 `# 註解`**。這個檔案不會掛進任何容器：`scripts/install.sh` 會在安裝時把它渲染
進 unit 檔與兩個容器的設定檔（決策 D2），所以改完之後要再跑一次 `scripts/install.sh`。

| 設定鍵 | 用途 |
|---|---|
| `WOOW_HEADSCALE_BIND` / `_PORT` | 控制平面發布在哪（`127.0.0.1`、本機某個位址，或 `all`） |
| `WOOW_HEADSCALE_METRICS_BIND` / `_PORT` | Prometheus 端點 |
| `WOOW_HEADPLANE_BIND` / `_PORT` | 管理介面 |
| `HEADSCALE_SERVER_URL` | **客戶端實際連線的 URL。** 是這台主機前面的反向代理或 port-forward，不是容器的埠 |
| `HEADSCALE_BASE_DOMAIN` | MagicDNS 後綴。不可等於或包含 `HEADSCALE_SERVER_URL` 的主機名，否則 headscale 拒絕啟動 |
| `HEADSCALE_IPV4_PREFIX` / `_IPV6_PREFIX` | 配發給節點的位址池 |
| `HEADSCALE_LOG_LEVEL` | `trace`…`error` |
| `HEADSCALE_CREATE_DEFAULT_USER` | 首次安裝時建立 headscale 使用者 `default` |
| `HEADPLANE_COOKIE_SECURE` | 只有在 Headplane 前面有東西終結 TLS 時才設 `true` |

每個值都由 `scripts/render-args.sh` 在**寫出任何檔案之前**驗證 — 錯誤的 bind 位址、埠衝突、帶路徑
或帶帳密的 `server_url`、會把 server URL 吃掉的 base domain，都會中止安裝且不留下任何變更。
`tests/dryrun.sh` 在 CI 逐一證明這些拒絕行為。

### 加入節點

```bash
podman exec headscale headscale preauthkeys create --user default --reusable --expiration 24h
# 在客戶端：
tailscale up --login-server "$HEADSCALE_SERVER_URL" --authkey <key>
```

### 對外連線

Tailscale 控制協定（TS2021）是帶有非標準 `Upgrade:` 標頭的 `POST`，之後接 Noise 握手。
**Cloudflare tunnel 會弄壞它** — 它會移除該標頭，客戶端在 `/machine/register` 失敗。因此預設把埠
發布在 loopback，前面再擺一條協定安全的路徑：路由器 port-forward 到 nginx／Traefik／NPM，並確保
`Upgrade` 與 `Connection` 原樣轉送、`proxy_buffering off`。完整的實測相容性表格見
[`docs/EXTERNAL-ACCESS.md`](docs/EXTERNAL-ACCESS.md)。

compose 部署另外附了一個選用的 **ngrok** sidecar 供快速打通用。Quadlet 部署不納入它：免費方案的
URL 每次重啟都會變，而在會自動重啟的 unit 底下，渲染好的 `server_url` 會在任何一次重開後就失效。
需要的話仍可在 `compose-final` tag 取得。

## Secret

本倉庫、`~/.config` 以及任何 unit 檔案中都不存放任何機密。Headplane 的兩個憑證是 podman secret，
以唯讀 0400 掛載：

| Secret | 來源 | 掛載位置 |
|---|---|---|
| `headscale-headplane-cookie` | `scripts/install.sh` 產生的 32 個隨機字元 | `/etc/headplane/cookie-secret` |
| `headscale-headplane-api-key` | 控制平面啟動後由 `scripts/install.sh` 執行 `headscale apikeys create --expiration 3650d` | `/etc/headplane/api-key` |

每次安裝都會拿 `headscale apikeys list` 驗證這把 key，若遺失、被撤銷或過期就重新產生；
`scripts/install.sh --rotate-api-key` 可強制更換。這把 key 不會出現在任何參數列：它被收進 shell
變數後以「變數名稱」交給函式庫，驗證器則從 stdin 讀取。`tests/smoke.sh`（檢查 A7）會確認兩個
secret 都沒有出現在 `podman inspect`、行程參數、journal、容器日誌或任何被追蹤的檔案中。

兩個容器都**沒有 `EnvironmentFile=`**：所有 per-host 值都以渲染好的設定檔進入容器，因此不會透過
`podman inspect` 外洩。

## 驗證

```bash
tests/smoke.sh              # unit、健康、發布的埠、HTTP、API key、secret 衛生
tests/smoke.sh --quick      # 只檢查 unit、健康、埠與 HTTP
systemctl --user status headscale.service headplane.service
journalctl --user -u headscale.service -f
```

## 升級

```bash
git pull && scripts/upgrade.sh
```

備份 → 快照已安裝的 unit → `scripts/install.sh`（會在動到任何 unit **之前**先建置並拉取新的釘選
映像）→ `tests/smoke.sh`。若安裝或煙霧測試失敗，會把快照的 unit 放回去，並以先前的映像重新啟動。

版本只釘選在這個倉庫裡 — `quadlet/*.container`、`Containerfile` 與 `scripts/common.sh` 必須一致，
`tests/lint-repo.sh` 會在它們不一致時讓 CI 失敗。請注意 Headscale 啟動時會遷移 SQLite schema 而且
**不會往回遷移**：跨 schema 變更時光把 unit 退回去是不夠的，請用 `scripts/restore.sh` 還原升級前的
封存檔。它的路徑會在每次升級一開始就印出來。

## 備份與還原

```bash
scripts/backup.sh                      # 封存檔路徑印在 stdout
scripts/backup.sh --include-secrets    # 連 cookie secret 與 API key 一起存
scripts/restore.sh --archive ~/.local/share/woow-backups/headscale/headscale-<stamp>.tar \
                   --confirm-restore headscale [--restore-secrets] [--restore-config]
```

擷取期間兩個容器都會停止，因此採用 write-ahead log 的 SQLite 資料庫會是一致的時間點。封存檔包含
`headscale-data` 與 `headplane-data` 兩個 volume 的匯出、渲染後的設定、`metadata.json` 與
`SHA256SUMS`；`headscale-run` 不在其中，因為它只放 gRPC unix socket。

還原會先用封存檔自己的 `SHA256SUMS` 驗證，再在停機狀態下做一份「還原前備份」；若在第一個破壞性
步驟之後失敗，會自動把那份備份回滾。還原後的節點保留原有金鑰，只要 `HEADSCALE_SERVER_URL` 仍指向
這台主機就會自動重新連線，不需重新註冊。

## 移除

```bash
scripts/uninstall.sh                                     # 只移除 unit，資料全部保留
scripts/uninstall.sh --purge --confirm-purge headscale   # 連 volume、網路、secret、設定一起刪
```

`--purge` 是本倉庫刪除資料的唯一途徑，而且會先做一份包含兩個 secret 與設定檔的冷備份。刪掉
`headscale-data` 等於所有節點都要重新加入。映像與備份永遠不會被刪除。

## 為什麼沒有 `migrate-legacy.sh`

其他十六個 WOOWTECH podman 倉庫都附了 `scripts/migrate-legacy.sh`，用來就地接管執行中的 compose
部署。這個倉庫刻意不提供，因為**沒有任何執行中的東西可以接管**：

* 在 `woowtechopenclaw` 上，`woow_headscale.service` 是**停用且未執行**的，`headscale` 與
  `headplane` 容器都不存在，三個 compose volume 已成孤兒；
* 其他 WOOWTECH 主機都沒有跑這個 stack 的 podman 版本。

寫一條接管路徑，等於交付並要求別人信任一支從未對真實部署跑過的資料搬遷腳本。取而代之的是：

* 只要任何 compose 時代的 unit（`woow_headscale.service`、`woow_headscale_health.{service,timer}`）
  仍在執行，`scripts/install.sh` 就**拒絕啟動**；若有非 Quadlet 管理、名為 `headscale` 或
  `headplane` 的容器，它也會拒絕接管，並印出 `podman rename` 指令，而不是讓
  `podman run --replace` 把它刪掉；
* Quadlet 的 volume（`headscale-data`、`headscale-run`、`headplane-data`）刻意**不沿用** compose 的
  名稱（`woow_headscale_*`），所以在曾跑過 compose 的主機上安裝是乾淨的並存安裝，舊資料完全不動；
* 萬一真要把資料從 compose 主機搬過來：在那邊對 `woow_headscale_headscale-data` 執行
  `podman volume export`，在這邊 `podman volume import` 進 `headscale-data`，兩邊的 unit 都要停止。

### 從 compose 部署轉過來

```bash
systemctl --user disable --now woow_headscale_health.timer woow_headscale_health.service woow_headscale.service
scripts/install.sh
```

舊的容器、volume 與映像都不會被動到；等 Quadlet stack 穩定一段時間後再自行清除。

## 映像與版本

`localhost/woow-headscale` 由 `scripts/install.sh` 在主機上依 `Containerfile` 建置（決策 D3 —
目前還沒有 WOOWTECH registry 映像；透過 CI 發佈到 GHCR 是後續工作）。建置是兩段式複製：headscale
執行檔與 CA 憑證取自以 digest 釘選的 `docker.io/headscale/headscale:v0.29.3`，疊在同樣以 digest
釘選的 `debian:12.11-slim` 上。加上 Debian 層的唯一理由是提供 `/bin/sh`：Quadlet 的 `HealthCmd=`
會變成 `podman run --health-cmd`，而 podman 是透過 shell 執行它的，上游的 ko 映像裡沒有 shell。
headscale 執行檔本身就是未經修改的上游版本。建置 context 只有 `Containerfile` 一個檔案，所以這份
checkout 的任何檔案都不可能進到映像層裡。

Headplane 從 GHCR 拉取，同時以 tag **與** digest 釘選。全倉庫沒有使用任何浮動 tag（決策 D4）。

## 安全性說明

* 預設所有埠都發布在 `127.0.0.1`；管理介面與 metrics 端點應維持如此，透過代理或 SSH tunnel 存取。
* 兩個容器都設定 `NoNewPrivileges=true`。
* `/etc/headscale` 與 `/etc/headplane/config.yaml` 皆以**唯讀**掛載，`tests/smoke.sh` 會驗證。
* `config/templates/headscale/policy.json` 沿用已驗證部署所使用的寬鬆起始政策 — 對所有節點
  `accept *:*`，並為 `default` 使用者自動核准 RFC1918 子網路由與 exit node。**在讓不受信任的裝置
  加入之前請先收緊**，改完後再跑一次 `scripts/install.sh` 以安裝新政策並重啟控制平面。
* metrics 端點沒有驗證：請保持在 loopback。
* `tests/lint-repo.sh` 會在出現任何憑證賦值、任何 Headscale 金鑰形狀的字串、ngrok auth token，
  或設定範例中值後面的行內註解時讓 CI 失敗。

## 檔案配置

```
Containerfile                       本機建置的 headscale runtime 映像
quadlet/*.container|volume|network  帶 @@TOKEN@@ 佔位符的 unit
quadlet/render-vars                 install.sh 可代換的 token 白名單
config/headscale.env.example        per-host 設定，複製到 ~/.config/headscale/
config/templates/**                 兩個容器的設定檔，安裝時渲染
config/templates/render-vars        上述模板的白名單（刻意與 unit 分開）
scripts/install.sh upgrade.sh uninstall.sh backup.sh restore.sh
scripts/common.sh headscale-helpers.sh render-args.sh validate-api-key.py
scripts/lib/quadlet-lib.sh          vendored 共用函式庫（決策 D8）— 不可在此修改
tests/dryrun.sh dryrun.local.sh     CI 中執行真正的 Quadlet 產生器與 systemd-analyze
tests/smoke.sh lint-repo.sh test-validate-api-key.py
docs/EXTERNAL-ACCESS.md             如何在不破壞 TS2021 的前提下對外開放控制平面
```

## 疑難排解

| 症狀 | 原因 |
|---|---|
| 客戶端註冊成功卻連不上 | `HEADSCALE_SERVER_URL` 不是客戶端連得到的位址。改正後再跑 `scripts/install.sh` |
| `/machine/register` 回 `500` | 前面有東西改寫了 HTTP upgrade — Cloudflare tunnel 就會。見 `docs/EXTERNAL-ACCESS.md` |
| headscale 一啟動就結束 | `HEADSCALE_BASE_DOMAIN` 包含了 `HEADSCALE_SERVER_URL` 的主機名。安裝器會擋，但手改過的已安裝設定檔不會 |
| Headplane 登入後又跳回登入頁 | 前面沒有 TLS 卻設了 `HEADPLANE_COOKIE_SECURE=true` |
| `headplane.service` 不斷重啟 | API key 不見了。執行 `scripts/install.sh --rotate-api-key` |
| `install.sh` 拒絕啟動 | compose 時代的 unit 還在跑，或有外來容器占用名稱。訊息會印出確切指令 |

## 相關倉庫

* [`Woow_k3s_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_k3s_vpn_headscale_package) — 多租戶的 Kubernetes/K3s 版本
* [`Woow_ha_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package) — Home Assistant OS add-on
* [`Woow_podman_vpn_tailscale_package`](https://github.com/WOOWTECH/Woow_podman_vpn_tailscale_package) — 同一套 Quadlet 標準下的 tailnet *節點*（客戶端）
