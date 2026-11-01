# 開發過程紀錄

這份文件記錄 API Traffic Monitoring 怎麼被做出來。它寫給兩種讀者。一種是專案的擁有者，幾個月後要回頭查某個決定的來源。另一種是想知道 AI 輔助開發在這裡怎麼被管理的 reviewer。

所有時間都是 CST（UTC+8）。決策紀錄 `decisions.tsv` 用 UTC 記時間，這裡已換算。`.audit/` 資料夾不在 repository 裡，文中只用檔名指稱。這份文件是整個 repository 裡唯一會點名工具與模型的地方。

## 1. 摘要

專案在約 5 小時 10 分內完成實作與驗收。計時從決策紀錄的第一列（10 月 31 日 23:37）到第一次驗收與文件交付完成的最後一列（11 月 1 日 04:47）。之後的審查、修正與第二次驗收從 05:03 到 06:20，所以到決策紀錄的最後一列（06:20）共約 6 小時 43 分。設計文件的撰寫時間不在內，因為沒有記錄。

過程是一個 coordinator 加六個 implementation owner。coordinator 是一個 Claude Code session，跑在 fable 模型上。它規劃、寫任務說明（brief）、親自驗證、合併。每個 milestone 由一個獨立的 owner 在自己的 git worktree 裡實作。M01 到 M05 的 owner 用 opus。文件和獨立驗收用 sonnet。

認證（M02）與即時推送（M03）各做了一次三模型對抗審查（opus、fable、sonnet 各一位 reviewer），再各做一輪修正。M02 的修正輪有 18 個 commit，M03 有 17 個。M04 與 M05 原本依計畫略過審查，在第一次驗收與文件交付之後才補做，一起審查，修正輪有 15 個 commit（第 5.8 節）。因為修正輪改了匯入、匯出、管理與 schema 的程式，驗收表在 `11c00dc` 上重跑了一次（第 7.2 節）。

最終結果在 `main` 的 `ca9e2c6`。第二次驗收的 commit 是 `11c00dc`（第一次驗收在 `2798239`）：

| 項目 | 數字 |
|---|---|
| commit 數 | 118（`ca9e2c6`）。第一次驗收時是 85（`2798239`），文件合併後 88（`babd5e4`） |
| 後端測試 | 394 passed（`11c00dc`）。第一次驗收時是 339 |
| 前端測試 | 17 tests OK（`11c00dc`）。第一次驗收時是 11 |
| 驗收表 | 20 PASS、0 FAIL（19 項在 `11c00dc`，DEP-03b 是 `e2bd097`；CI-01 是 push 後的 GitHub Actions run 41273501794，`ff0cdce`） |

CI-01 在擁有者 push 之後於 GitHub Actions 執行：run 41273501794（`ff0cdce`，2026-11-01 04:32 UTC）三個 job 都 success，lint 9 秒、test 2 分 27 秒、frontend-test 14 秒。本文件後面各節寫的 NOT RUN 是當時的狀態。

## 2. 出發點

### 2.1 題目與期限

[plan.md](plan.md) 記錄的截止時間是 2026-11-01 17:00。計畫把工作分成三個優先級。P0 是能從乾淨環境啟動、三個帳號權限不同、即時圖會動、停 DB 30 秒後恢復且 drain 後無重複。P1 是匯入、統計、管理和關鍵測試。P2 是 UI 細節、CI 和操作錄影。計畫寫明 P0 沒完成前不碰 P1。

計畫把工作拆成六個 milestone，預算合計約 15 小時（M01 2.5 h、M02 3 h、M03 4 h、M04 2.5 h、M05 2 h、M06 1 h）。實際實作用了約 5 小時 10 分。這份文件不解釋差距的原因，因為沒有資料支持任何解釋。

### 2.2 先寫文件，再寫程式

在第一行程式出現之前，下列文件已經存在，並在 `6f395f9`（10 月 31 日 23:34）成為 `main` 的第一個 commit：

- [architecture.md](architecture.md)
- 三份 ADR，在 [decisions/](decisions/)（ingestion buffer、認證與 RBAC、即時推送）
- [CLAUDE.md](../CLAUDE.md)
- [plan.md](plan.md) 與 [testing.md](testing.md)

這些文件的撰寫時間沒有記錄，所以本文不給時數。[ai-review-log.md](ai-review-log.md) 的 R-001 到 R-003 記錄了對設計文件本身的三筆審查。

這個順序有三個理由，都來自 `decisions.tsv` 與 `todo.md`。

1. 決策紀錄第一列說，設計已由 ADR-001 到 003 固定，所以 `how`、`architect`、`arena` 不會增加資訊。
2. 每個 owner 的 brief 都要求它先讀 CLAUDE.md 與相關 ADR。ADR 是 owner 的規格，也是 reviewer 判斷「這是 bug 還是已決定的取捨」的依據。M03 審查中有兩項被駁回，理由都是「ADR 已明確決定」（見第 6 節）。
3. [testing.md](testing.md) 的驗收表在實作前就存在。獨立驗證者直接照表執行（第 7 節）。

## 3. 開發方式

### 3.1 角色分工

| 角色 | 做什麼 | 模型 |
|---|---|---|
| coordinator | 規劃、寫 brief、驗證、合併、記錄決策 | fable |
| implementation owner（M01 到 M05） | 在自己的 worktree 實作一個 milestone，寫 report | opus |
| 修正輪 owner（M02、M03） | 套用審查後接受的項目 | opus |
| 修正輪 owner（M04 與 M05） | 套用第三輪審查接受的 15 項 | 紀錄裡沒有寫 |
| 審查者（每次 interrogate 三位） | 唯讀審查一個 commit 範圍 | opus、fable、sonnet 各一 |
| 獨立驗收者 | 在 detached worktree 上執行驗收表 | sonnet |
| 文件與本文件的 owner | 寫 README、本文件 | sonnet |
| 擁有者（人） | 設計、scope 取捨、`gh auth login` 與 GitHub push | 不適用 |

規則有三條。coordinator 不改程式樹，只合併。每個 owner 只在自己的 worktree 工作。合併一律是 fast-forward 進本機 `main`。

### 3.2 工具與流程

工具是 Claude Code。流程是 pstack 技能組的 Feature playbook（`poteto-mode`）。owner 以 `pstack:poteto-agent` 啟動。決策紀錄依 `show-me-your-work` 技能寫成 TSV，每個決定一列，欄位是時間、階段、決定、理由、證據、結果。

`todo.md` 把 Feature playbook 的八個步驟逐一對應到這個專案：

| 步驟 | 做了嗎 | 說明（出自 todo.md） |
|---|---|---|
| 1 `how` | 略過 | M01 是 greenfield，沒有東西可探索。M02 之後由 owner 在 brief 的第一步讀前一個 milestone 的程式。 |
| 2 `architect` | 略過 | 設計已由 ADR 與 architecture.md 固定，對已定案的設計再開一次 arena 是過度設計。 |
| 3 throughput checkpoint | 做了 | 寫成四個 todo 項目。milestone 因相依而循序進行（M02 的 JWT 供 M03 的 WS 認證使用）。 |
| 4 委派實作 | 做了 | 每個 milestone 一個 owner，因為 compose、config、`main.py` 把所有模組綁在一起。 |
| 5 在對應介面驗證 | 做了 | coordinator 用 docker compose（在 Podman 上）、curl、容器內 pytest、ruff。 |
| 6 整理成小而有序的 commit | 做了 | owner 提交邏輯單元，coordinator 做 fast-forward 合併。 |
| 7 `interrogate` | M02、M03，之後補做 M04 與 M05 | M04 與 M05 原本依計畫略過（見 3.4），在第一次驗收之後補做（見 5.8）。 |
| 8 Opening a PR | 略過 | 擁有者自己 push 到 GitHub，commit 留在本機 `main`。 |

`todo.md` 的最後一個未勾選項目是「show-me-your-work audit + cross-model review of the trail」，也就是對這份決策紀錄本身的審查，在寫作本文時尚未進行。

### 3.3 模型選擇

依難度分配，來源是 `todo.md` 與決策紀錄：

- M01 到 M05 與兩個修正輪用 opus。這些是有狀態、要處理並行與故障的實作。
- README、testing.md、本文件與獨立驗收用 sonnet。決策紀錄寫的理由是這些是簡單工作，由擁有者指定模型分配。
- 審查用三個模型家族各一位。決策紀錄寫「跨模型家族的共識是訊號」。

### 3.4 為什麼 M04 與 M05 起初不審查

後來補做了審查（見 5.8），結果是 1 個 critical 與 14 個其他採納項目，所以下面這個判斷事後看是錯的。當時的理由如下。決策紀錄寫得很直接。M04 是「建在已審查過的 auth 與 records service 之上的資料管線」。M05 是「建在已審查過的 auth 之上的 admin CRUD，保護規則有 MariaDB 上的競爭測試」。這是風險判斷，不是證明。M05 的 owner 自己發現了 1020 問題（見 5.5），這代表沒有審查也找得到問題，但不代表沒有審查就沒有遺漏。補做的審查證明了後半句。

### 3.5 每個 milestone 的固定動作

1. coordinator 寫 brief。brief 包含目標、必讀文件、驗證步驟與禁止事項。
2. owner 先在 report 裡列出「預計變更範圍與風險」（CLAUDE.md 工作規則 1），再實作。
3. owner 依 CLAUDE.md 工作規則 5 只回報真的執行過的命令與 exit code，沒跑的標 NOT RUN。
4. coordinator 在 owner 的 worktree 上自己重跑一遍（第 5 節列出每次的命令與結果）。
5. 需要時送 interrogate，審查結果寫成 synthesis，分成 Act on、Consider、Noted、Dismissed 四類，再派修正輪。
6. coordinator 在決策紀錄新增一列並 fast-forward 合併。

## 4. 環境

專案要在沒有 Docker Desktop 的 macOS 上開發。README、testing.md 與 brief 都使用同一組 `docker compose` 命令，因為評審者也會用它們。所以做法是安裝 docker CLI，讓它對 Podman 說話。

| 時間 | 事件 | 處理 |
|---|---|---|
| 23:34 | `git init` 並提交設計文件 `6f395f9` | 每個 milestone 的 worktree 需要一個 base commit。 |
| 約 23:37 | 用 brew 安裝 `docker`、`docker-compose`、`gh` | CLI 指向 Podman，不裝 Docker Desktop。`gh` 裝了但沒登入，因為登入是互動式的，只有擁有者能做。 |
| 約 23:37 | `podman machine start` 失敗，vfkit exit 1 | `podman machine list` 顯示 LAST UP 是 Never。這台 machine 建於三個月前，從未啟動，所以刪掉重建（`rm` 加 `init`，4 CPU、6 GiB），沒有東西遺失。 |
| 23:37 | 重建後啟動成功 | docker context 名為 `podman`。`podman info`、`docker info`、`docker pull mariadb:11.7` 都成功。 |
| 23:39 | 同一台 machine 的第一次開機，gvproxy 逾時，沒有 API socket | stop 再 start 就好。`docker info` 回報 server 5.7.1、fedora、4 CPU、6 GiB，證據是 gvproxy.log。 |

決策紀錄裡有兩列描述 Podman 重建，其中一列的結果是 `pending`，另一列說「取代上面的 pending 列」。兩列的時間只差約兩分鐘。我讀到的是同一件事被記了兩次，後一列補上第一次開機的細節。

另外有四件事記在 report 裡，與環境有關：

- Python 用 uv。後端與前端各有一個 uv project，Dockerfile 以 `ghcr.io/astral-sh/uv:0.8.22` 做 multi-stage build。
- Podman 會把 exec 形式的 healthcheck 拆開，`python -c` 只收到 `import`，所以 healthcheck 改用 `CMD-SHELL`。M01 report 說原因是推測，沒有查證，也沒有在 Docker Engine 上跑過。
- Podman 的 `docker compose ps` 在 STATUS 欄不顯示 health，M02 owner 改用 `docker inspect` 確認。
- zsh 沒有 `PIPESTATUS`。M02 修正輪 owner 因此有一次 baseline 的 exit code 沒取到，之後的命令都開 pipefail。

任務說明（brief）要求我寫「zsh word splitting」。我讀過的所有來源都沒有記錄這件事的細節，所以不寫經過與時間。與 zsh 有關、有記錄的只有上面的 `PIPESTATUS`，以及驗收者第一次 RETRY-01 的 WS client 輸出因為 shell 的 noclobber 錯誤而遺失（第 7 節）。

## 5. 逐里程碑

### 5.1 M01 Foundation

**時間。** coordinator 在 23:39 啟動 owner。owner 的 commit 在 23:56 到 23:59。coordinator 在 00:03 驗證並合併。約 24 分鐘。計畫預算是 2.5 小時。

決策紀錄說 M01 合併在 00:03。這份文件的任務說明寫 00:00。我用決策紀錄的時間，因為它有秒。

**目標。** 讓 `docker compose up --build` 起三個服務，`/health/ready` 回 200，`traffic_test` 存在，時間欄位 round-trip 與 migration 在真實 MariaDB 上通過。

**做了什麼。** compose 起 `db`（mariadb:11.7）、`api`、`frontend`。API 有 JSON log、request id、統一錯誤 envelope、`/health/live` 與 `/health/ready`。schema 是五張表加一個 `UTCDateTime` 型別。前端首頁顯示專案名稱與 readiness 狀態。8 個 commit。

**owner 自己抓到的問題。**

- MariaDB downgrade 時 drop index 的順序錯（錯誤碼 1553）。
- Podman 上 exec 形式 healthcheck 失效，改 `CMD-SHELL`。
- 重複的 access log（uvicorn 與 app 各寫一行），關掉 uvicorn 那一行。

這三項記在 R-004，因為決策紀錄說 review log 只能放真的發生過的事，而它們來自 owner 自己的驗證。

owner 還做了 mutation check。拿掉 `astimezone(UTC)` 後 TZ-01 失敗。拿掉 naive 檢查後失敗於 `DID NOT RAISE StatementError`。兩處還原後 12 passed。

**coordinator 驗證。** 在 Podman 上跑：

- `up --wait` 後三個服務 healthy。
- `/health/ready` 回 200 與 `HEALTHY`，`/docs` 回 200。
- 容器內 `pytest` 12 passed。
- `ruff` 乾淨。
- 404 的 envelope 帶 `request_id`。

**審查。** 沒有送 interrogate（greenfield，設計已固定）。

**決策。** coordinator 接受 compose 的 api image 用 `INSTALL_DEV=true`，理由是 testing.md 與 README 要在 api 容器內跑 pytest 與 ruff，另做一個測試 image 對這個專案是多餘的手續。這一點寫進 README 的已知限制。

**合併。** `252d83a`。M01 共 8 個 commit，加上 base commit 共 9 個。

### 5.2 M02 Auth / Records

**時間。** M01 合併後約 21 分鐘，owner 的最後一個 commit（`b8faf46`）在 00:24。owner 的啟動時間沒有記錄。這個數字是上限的推論，不是量測。coordinator 在 00:30 驗證完畢並派三位 reviewer。審查結論在 00:40。修正輪在 01:14 合併。從驗證到合併 44 分鐘。

**目標。** 實作 ADR-002：Argon2id、HS256 JWT、三種角色、Ownership（越權回 404）、Records CRUD 與 bulk、threshold snapshot、同交易稽核、啟動時的 bootstrap admin 與 demo 帳號。

**做了什麼。** 8 個 commit，120 個測試。

**owner 自己抓到的問題。** owner 的 mutation check 找出一項測試抓不到的改動。拿掉遞增排序的 id 次排序後，0 個測試失敗。原因是 MariaDB 剛好依主鍵順序回傳相同值。owner 照實寫進 report。第一輪 mutation 也有一次結果不可信，owner 推測是 stale `.pyc` 造成，清掉 `__pycache__` 並關閉 bytecode 寫入後重跑（這個原因是推論，report 也標了未逐一查證）。

另有一個文件缺口由 owner 提出。architecture.md §6 的排序白名單有 `created_at`，但 §5.1 的表沒有這個欄位。owner 給了 A（保留對應並寫進 §6）、B（改名為 `persisted_at`）、C（新增欄位）三個選項，建議 A。

**coordinator 驗證（`b8faf46`）。** pytest 120 passed，ruff 乾淨。curl 矩陣：viewer 寫入 403、跨使用者 404、register 帶 role 422、`alg=none` 401、`page_size=101` 422、threshold 為嚴格大於、email 小寫化。稽核列存在，log 中沒有 secret。

**coordinator 決策。** 選 B。決策紀錄寫的理由是排序鍵應該是 Read schema 實際回傳的欄位，別名會讓 API 使用者困惑，且 A 與 B 的改動量相同。

**審查。** 三位 reviewer 唯讀審查 `252d83a..b8faf46`。見第 6 節。結論是沒有 critical，16 項採納。

**修正輪。** 18 個 commit（`ccd44a7` 到 `964ea67`，00:49 到 01:04）。主要項目：

- Argon2 移出 event loop，密碼上限 128。
- 一個 handler 把連線類錯誤對應成 503。
- 每個 401 帶 `WWW-Authenticate: Bearer`。
- `verify_password` 接住 `InvalidHashError`。
- 排序鍵改名為 `persisted_at`。
- 測試補強，owner 另外跑了 31 個 mutation，30 個被抓到。

漏掉的是 RS256 白名單那一個。原因是專案沒有安裝 `cryptography`，PyJWT 在白名單作用前就先拒絕 RS256。這個測試在簽章驗證被關掉時仍會失敗，所以保留。

owner 在第 14 項偏離了 synthesis 的字面。synthesis 要求從對照表衍生 Literal，owner 反過來讓 `RecordSort` 成為唯一來源並刪掉對照表。理由有兩個。repository 已 import schemas，反向 import 會造成循環。改名之後三個鍵都和欄位同名，對照表只剩恆等對應。coordinator 接受。

owner 量了效能。移出 event loop 之後，4 個登入進行中時 `/health/live` 的 median 從 46.7 ms 降到 1.2 到 1.5 ms。單一登入的 median 約 27 ms，沒有變，因為限制在 CPU。一次 verify 用掉約 3.6 核。這代表登入吞吐量沒有改善，rate limit 仍是 ADR-002 的 open item，列入已知限制。

**coordinator 驗證（修正後，head `964ea67`）。**

- pytest 163 passed。
- `sort=persisted_at` 回 200，`sort=created_at` 回 422。
- 401 帶 `WWW-Authenticate`。
- 空 PATCH 回 422，`start > end` 回 422。
- 真的 `docker compose stop db` 之後，`/auth/token`、`/records`、`/health/ready` 回 503 envelope，db 啟動後回到 200。
- log 中沒有 secret。

**合併。** `964ea67`，01:14。M02 共 26 個 commit（owner 8 加修正 18）。決策紀錄寫「26 commits total, head 964ea67」，指的是 M02 自己的 26 個，不是 `main` 的總數。`git rev-list --count 964ea67` 是 35，包含 M01 的 9 個。

### 5.3 M03 Realtime

**時間。** owner 的 commit 在 01:30 到 01:55。coordinator 在 02:03 驗證（決策紀錄的 18:03Z 列）。審查結論在 02:22。修正輪 commit 在 02:34 到 03:08。合併在 03:16。從驗證到合併 73 分鐘。計畫預算是 4 小時，這是最難的 milestone。

**目標。** 有界 ingestion buffer、batch writer、simulator、WebSocket 推送，加上前兩個 Streamlit 頁（Login 與 Live Monitor）。

**做了什麼。** 13 個 commit，200 個測試。buffer 單元測試先寫（`a133e78`），再補實作（`4800183`、`7b39997`）。這是刻意的。

**owner 自己抓到的問題。**

- owner 的第一輪 mutation 有兩條沒被抓到：「`queue.get()` 當 ack」與「reserve 與 enqueue 之間多一個 await」。owner 因此新增 `test_cancelling_accept_at_any_await_point_never_leaks_capacity`（`293c3af`），並修正 mutation 本身。最後 8 個 mutation 全被抓到。
- owner 實測了「accept 之前 close」。在同版 uvicorn 與 websockets 上，client 收到 HTTP 403 handshake rejection，收不到 4401。所以程式先 accept 再 close。
- owner 先做了 Streamlit 最小原型，確認 receiver thread 在兩次 script run 之間持續接收，才加圖表。這是 plan.md 的第 6 步。

**coordinator 驗證（`7d8916f`）。**

- pytest 200 passed，ruff 乾淨。
- WS 無 token 與壞 token 都以 4401 關閉。
- 有效 token 的第一則是 status，接著 6 秒內收到 19 則事件，鍵符合 ADR-003。
- 停 DB 28 秒。`/health/ready` 回 503 DEGRADED，uncommitted 從 24 到 48 到 75，writer 在 RETRY_WAIT。
- 恢復後 10 秒內回到 HEALTHY。資料列 90/90 在停機前，219/219 在恢復後（筆數等於不重複 event_id 數）。
- log 有 `writer.recovered`。

**coordinator 自己的疏漏。** 這次演練是在 `stop db` 之後才連 WS client。所以它驗的是「新連線」路徑，不是 ADR-003 保證的「既有連線」路徑。決策紀錄當天就寫下這一點，狀態是 open。握手時 DB 不可用會被關成 1013，這是新連線的行為。修正輪的驗證改成先連線、再停 DB（見下）。

**審查。** 三位 reviewer 審查 `964ea67..7d8916f`，聚焦並行與容量計算。結論是 3 個真正的 critical 採納，另有 13 個 act-on 項，共 16 項。見第 6 節。

**coordinator 決策。** 兩項。第一，writer 進入 UNAVAILABLE 時 generator 在一個 tick 內停止（ADR-001 明寫連帶停止）。第二，DB 不可用時的握手關成 1013，並在 ADR-003 補一句話，因為這是 ADR 的缺口，不該留給程式碼決定。

**修正輪。** 17 個 commit（`0de861a` 到 `6fd52c2`）。主要項目：

- writer 錯誤改依 errno 與 `connection_invalidated` 分類。
- 每次 commit 嘗試加 timeout，且 `evaluate()` 在 buffer 滿且 writer 久未 commit 時回報 UNAVAILABLE。
- generator 在 writer 停止後一個 tick 內停止。
- 關閉時 back-off 被喚醒，做最後一次嘗試。
- 慢 client 由 cleanup task 以 1008 關閉，每次 send 有 timeout。
- token 過期的閒置連線以 4401 關閉。
- 健康狀態不再抖動。
- 前端 receiver 任何例外都不死，重連有 jitter。

owner 不同意或沒做的部分：REST 的 503 handler 沒有依 errno 細分（synthesis 的括號寫可以維持依 class 判斷），留給 M05 處理。frontend 測試沒接進 CI，也留給 M05。

**coordinator 驗證（修正後，head `6fd52c2`）。**

- pytest 242 passed，ruff 乾淨。
- 主機上 frontend unittest 7 ok。
- `StopTimeout` 20。
- 既有連線演練。client 先連線，停 DB 25 秒。client 持續收到 166 則不重複事件，狀態從 DEGRADED 回到 HEALTHY，連線沒有被關。
- 恢復後 `ready` 先回 503 DEGRADED（uncommitted 36 到 60 到 87），DB 啟動後 16 秒內回到 HEALTHY。
- 資料列 108/108，之後 261/261。
- log 有 `writer.recovered`。

**一件要誠實記下的事。** 修正輪演練時，一個本機 demo 用的 VIEWER JWT 被印進 owner 的工具輸出。這個 token 在 11 月 1 日 03:04 UTC 過期，每個 worktree 的 JWT secret 隨 worktree 刪除而消失。決策紀錄把這件事列為 noted，不需要額外處理，只要不要重用那個 `.env`。

**合併。** `6fd52c2`，03:16。M03 共 30 個 commit（owner 13 加修正 17）。

### 5.4 M04 Data

**時間。** commit 在 03:30 到 03:47。合併在 03:53。

**目標。** CSV 與 JSON 匯入（All-or-Nothing）、四個 analytics endpoint、Excel 匯出、demo 資料，以及 Records 與 Analytics 兩個頁面。

**做了什麼。** 10 個 commit，291 個測試（新增 49 個）。

**owner 自己抓到的問題**（R-007）：

- 兩個測試檔同名，pytest 報 `import file mismatch`。改用 `--import-mode=importlib`。
- `data/` 目錄的路徑在本機執行時找不到。改成掛在 `/data`，程式用 `parents[2]`。
- 規格缺口。`data/sample_traffic.csv` 有三列固定 `event_id`。seed 匯入後，再匯入同一個檔案一定整批 409。這符合 architecture.md §5.3，但 brief 寫的「匯入 sample 檔得 201」在已 seed 的環境不成立。owner 沒有改規則，改用 seed 的 batch 與一份清空 `event_id` 的副本來證明。
- 一個 mutation（只寫有效列）被 `import_batches` 的 CHECK 擋下，不是被資料交易的檢查擋下。owner 的結論是資料交易沒有第二道防線，全靠先驗證所有列。
- 13 個 mutation 全被抓到。
- 效能。在 `traffic_test` 放 100k 列：summary 0.01 秒、minute trend 0.04 秒、export 33,333 列 1.56 秒。約 50k 列 export 期間，event loop 最長停頓 57 ms。

**coordinator 驗證。** pytest 291 passed，ruff 乾淨。seed 批次 15/15。原始 sample 檔回 409。清空 id 的副本回 201（15/15）。壞列回 422 並指出第 8 列的 value，`IMPORT` 筆數不變。固定 `end` 時，payment 的 summary count、records total、export 列數都是 10。categories 加總等於 summary count。viewer 匯入回 403，看 analytics 回 200。log 中沒有 secret。

**決策。** seed 改匯入 JSON 而不是 CSV，讓 demo 的 CSV 匯入在乾淨 stack 上能成功。決策紀錄記錄了依據。兩個檔案的固定 id 各 3 個，重疊 0 個。這件事指派給 M05。

**審查。** 當時略過（見 3.4）。之後與 M05 一起審查，見 5.8。

**合併。** `2e351e7`，03:53。

### 5.5 M05 Admin + Tests

**時間。** commit 在 04:02 到 04:24。合併在 04:32。

**目標。** `/admin/*`、薄的 Admin 頁，以及 testing.md 裡尚未自動化的案例。brief 也要求處理前幾輪留下的項目。

**做了什麼。** 10 個 commit，339 個後端測試（新增 48 個）與 11 個前端測試。REST 的 503 改用 errno 分類。CI 加了 frontend 測試 job。seed 改用 JSON。

**owner 自己抓到的問題**（R-008，共 10 項）：

- 最重要的一項是 MariaDB 1020。第一版 `update_user()` 在認證讀取之後直接 `SELECT ... FOR UPDATE`，兩個 last-admin 測試都得到 1020（`Record has changed since last read`），不是 422。原因是 MariaDB 11.7 預設 `innodb_snapshot_isolation=ON`。owner 改成先 commit 只讀的認證交易再上鎖。
- 拿掉 `FOR UPDATE` 的 mutation 也被抓到，但失敗內容是 1020（500），不是預期的「兩邊都成功」。
- 1020 不只影響 Admin。PATCH `/records` 與 `/services` 也是先讀再寫。owner 把 1020 加進暫時錯誤清單，REST 回 503，並加一個在真實 MariaDB 上觸發 1020 的 integration test。
- 預定加進 CI 的 `python -m unittest` 回 `NO TESTS RAN`，因為 `frontend/tests` 沒有 `__init__.py`。加上之後找到 11 個。
- 18 個 mutation 全被抓到。
- 一次未解釋的逾時。frontend 頁面測試第一次執行時 `AppTest` 在 10 秒逾時，之後 13 次都在 1 秒內完成。owner 推測是 macOS 第一次載入新 venv，沒有證實。

**coordinator 驗證。** pytest 339 passed，ruff 乾淨，主機上 frontend 11 tests。viewer 被升為 USER 後，同一個舊 token 的 `POST /records` 回 201。自己停用自己與自己降級回 422。email 變更回 422。非 admin 打 `/admin/*` 回 403。`/admin/system` 有 pool、table counts、readiness、generator stats。有 10 種稽核事件。壞的 `event_type` 回 422。CI 有 frontend job。seed 現在是 JSON 6/6，CSV sample 匯入回 201（15/15）。

**審查。** 當時略過（見 3.4）。之後與 M04 一起審查，見 5.8。

**合併。** `2798239`，04:32。

### 5.6 驗收

**時間。** coordinator 在 04:32 啟動一位 sonnet 驗收者。結果在 04:42。

驗收者在 `2798239` 的 detached worktree 上，用單一 compose stack 照 testing.md 的表依序執行。不開並行，因為只有一個 stack。驗收者不改任何 tracked 檔案。細節在第 7 節。

### 5.7 M06 文件

**時間。** 決策紀錄在 04:42 記下 README owner 的啟動，在 04:47 記下合併。文件 commit 的時間是 04:45。本文件的 owner 在 04:47 啟動，所以它能涵蓋前者。

**M06a（README、testing.md、review log）。** 三個 commit（`4f1ebb5`、`9b350e2`、`babd5e4`）。README 的狀態表、開發方式、已知限制與本地運行補齊。`.env.example` 移除 `DB_HOST`、`DB_PORT`、`CORS_ORIGINS`，因為沒有程式碼讀它們。testing.md 填入驗收證據。review log 補上 R-005 與 R-006 的多模型審查項目。plan.md 加上結果表。

owner 驗證了本地運行的修正。舊的 README 命令 `uv run --env-file ../.env alembic upgrade head` exit 1（`Can't connect to MySQL server on 'db'`），證實需要修正。新命令用 `.env.local`，exit 0。owner 沒有跑前端本機啟動命令與本機 pytest，也沒有在 Docker Engine 上驗證。

owner 另外回報：plan.md 第 5 行仍寫 commit message 描述「做了什麼、怎麼驗證」，但 commit 其實只有 subject。這一行在同一個 branch 的另一個 commit 被改掉（見下）。

plan.md 第 5 行的修正在獨立的 commit `docs: correct the commit-message note and the timeline label`。

**合併。** README 合併後 `main` 是 `babd5e4`（88 個 commit）。

### 5.8 M04 與 M05 的審查與修正輪

**時間。** coordinator 在 05:09 派出三位 reviewer，同時派出 CI 步驟重放（第 7.3 節）。決策紀錄在 05:28 寫下審查結論。修正輪的 15 個 commit 在 05:38 到 05:55。coordinator 在 06:11 驗證並合併為 `11c00dc`，同一時刻開始重跑驗收（第 7.2 節）。

**為什麼補做。** M04 與 M05 是唯一沒有對抗審查的 milestone。決策紀錄寫的理由是補上這一輪，Feature playbook 的第 7 步才涵蓋整個程式碼。審查範圍是 `6fd52c2..2798239`。

**審查結果。** opus 14 項（1 critical、7 warning、6 nit），fable 18 項（8 warning、10 nit），sonnet 11 項（7 warning、4 nit），共 43 項。唯一的 critical 由 opus 提出，而且三位 reviewer 都指出同一個問題。匯出的 Excel 把使用者的 title 原樣寫入，以 `=` 開頭的 title 會被 Excel 當成公式。含 C0 控制字元的 title 還會讓之後每次涵蓋那一列的匯出都回 500。opus 在拋棄式環境重現了這兩個結果。

**採納的 15 項。** 一項一個 commit。

- 匯出（`671ca02`）。每個文字值寫成文字儲存格，`Title` 在 schema 邊界拒絕 C0 控制字元，匯出失敗時清掉暫存檔。
- 匯入檔案層級的錯誤（`3b45cb4`）。巢狀 JSON、超過 12 位的整數、沒有資料列的檔案都回檔案層級 422。
- 匯入的 transaction（`90c0d01`）。成功路徑先寫 SUCCEEDED 的 batch，再寫資料列與稽核，一次 commit。`create_records()` 不再自己 commit，由呼叫端決定交易邊界。原本 batch 與資料列分兩段 commit，中間有空檔，三位 reviewer 都指出。
- 錯誤分類（`51b105c`）。暫時錯誤清單加入八個 errno。
- 索引（`1f20db7`）。migration `0002` 新增四個索引，涵蓋 `system_logs`、`traffic_records.observed_at` 與 `users`。
- `/admin/system`（`4844f04`）。各表筆數快取 10 秒。
- 管理者權限的新鮮度（`c784177`）。`update_user()` 鎖定之後確認操作者仍是有效 ADMIN，否則回 403。
- 稽核事件名稱（`2edd11c`）。`record_event()` 拒絕 `AuditEvent` 以外的名稱。
- Analytics 頁預設最近 24 小時（`3b9c55d`）。
- 上傳大小（`69aaad4`）。`Content-Length` 超過上限時，在讀 body 之前回 413。
- 冪等碰撞（`cdc410f`）。Writer 遇到被非 SIMULATOR 列佔用的 `event_id` 時記 WARNING。
- `observed_at`（`7791eba`）。拒絕 Unix time 數字。
- M04 頁面測試（`2dc1d68`）與覆蓋率（`09e077b`）。
- 文件（`11c00dc`）。architecture 的行號規則、README 已知限制、review log 的 R-007 與 R-008。

**驗證。** coordinator 驗證的結果是 pytest 394 passed（修正前 339，新增 55 個），frontend 17 tests（原本 11 個），ruff 乾淨，migration `0002` 的 downgrade、upgrade 與 `alembic check` 都乾淨。用 curl 驗證的項目是 `=HYPERLINK` 的 title 匯出後是文字儲存格，含 `\x01` 的 title 回 422，巢狀 JSON 回 422，只有標頭的 CSV 回 422，6 MB 的上傳回 413，自己降級回 422。owner 另外逐 commit 跑了 pytest，349 到 394 只增不減。owner 報告的 mutation 是 43 個，42 個被抓到，這個數字是 owner 自己報的，沒有被獨立重跑。

**保留「最後一個有效 ADMIN」規則的決定。** 加上操作者檢查與「不能動自己」之後，這條規則在 API 上已經走不到。owner 拿掉它做 mutation，29 個測試仍通過，所以那個 mutation 存活。owner 把這個選擇交給 coordinator。coordinator 決定保留，理由是 ADR-002 明文列出這條規則，保留只多兩行程式，而且兩個 integration test 現在斷言至少留下一個有效 ADMIN。

**owner 回報的其他限制。** 「關閉 workbook」照字面做沒有效果，真正刪暫存檔的是 openpyxl 內部 sheet writer 的 `cleanup()`，程式因此用到 openpyxl 的私有屬性。C0 範圍包含 tab 與換行，所以 title 現在不能含這兩個字元。413 早於認證，沒有 token 的超大上傳拿到 413，不是 401。

**合併。** `11c00dc`，06:11。修正輪共 15 個 commit，37 個檔案，新增約 962 行、刪除 156 行。

## 6. 多模型審查

### 6.1 做了什麼

審查用 `interrogate` 技能。每次三位 reviewer，模型各自不同（opus、fable、sonnet），唯讀，各自獨立。三位的審查結果由 coordinator 合成一份 synthesis。

| | M02（`252d83a..b8faf46`） | M03（`964ea67..7d8916f`） | M04 與 M05（`6fd52c2..2798239`） |
|---|---|---|---|
| opus（A） | 14 項（5 warning、9 nit） | 12 項（2 critical、5 warning、5 nit） | 14 項（1 critical、7 warning、6 nit） |
| fable（B） | 11 項（4 warning、7 nit） | 14 項（1 critical、5 warning、8 nit） | 18 項（8 warning、10 nit） |
| sonnet（C） | 15 項（7 warning、8 nit） | 16 項（聲稱 3 項 critical、10 warning、3 nit） | 11 項（7 warning、4 nit） |
| synthesis 的處置 | 16 採納、5 考慮、4 記錄、1 駁回 | 16 採納、2 考慮、2 記錄、2 駁回 | 15 採納、3 考慮、2 記錄、0 駁回 |

M03 的 sonnet 聲稱的 critical 與最後採納的 critical 不是同一組。採納的三項是 errno 分類、commit timeout、generator 停止。sonnet 的 poison quarantine 項被駁回（見 6.3）。

### 6.2 採納的規則

決策紀錄寫的規則有兩條。三個模型家族一致的項目直接採納。只有一個模型提出的項目，要有具體的執行路徑才採納。

M02 一致採納（3/3）：Argon2 移出 event loop、一個 503 handler、`WWW-Authenticate`、`InvalidHashError`。2/3：`hide_parameters`、bootstrap 密碼檢查、email 正規化、PATCH 稽核。1/3 但便宜又具體：placeholder secret、`OverflowError`、inactive services、`Writer` 改名。

M03 一致採納（3/3）：generator 停止、慢 client、關閉時的 back-off。單一模型但已用執行證明：errno 分類（opus 在純 Python 跑 asyncmy 例外）與關閉後殘留事件的競爭（fable 用真實類別重現）。

M04 與 M05 的共識地圖。三位都提出的有六項：匯出的公式注入、巢狀 JSON 造成 500、errno 缺口、匯入的 transaction 空檔、缺少索引、每次 rerun 都做 `COUNT(*)`。兩位提出的有四項：操作者權限的新鮮度檢查、上傳大小檢查太晚、空檔案被當成功、控制字元讓匯出失敗。只有一位提出但便宜又具體而被採納的有五項：`AuditEvent` 轉換、`observed_at` 的 epoch 數字、Analytics 的預設時間範圍、M04 頁面測試、冪等碰撞的 log。

三位 reviewer 都確認核心規則成立。M02 是 HS256 固定、必要 claim、角色由 DB 決定、越權回 404、登入失敗稽核先於 401。M03 是沒有人在取消時發現容量洩漏，accept-before-broadcast 與 commit 後才 release 都成立。

### 6.3 考慮、記錄與駁回

**M02 考慮（不在這一輪）。** PATCH 與 DELETE 的 row lock（單 worker demo，延後）、遞增排序的 id 次排序（MariaDB 無法強制，記在 R-005）、`flush_or_conflict` 的位置、repository 與 schema 的方向、`admin_exists` 計入停用的 admin（M05 的規則使它在 API 上不可達）。

**M02 駁回。** sonnet 質疑 `/auth/register` 是否該存在。駁回理由是題目的功能需求 1 要求使用者註冊，ADR-002 也有規定。

**M04 與 M05 考慮與記錄。** 考慮（不在這一輪）三項。重複的 `Page[T]`、時間區間驗證與 count 加 offset 的輔助函式；10,000 列匯入的 round trip 次數，只有推論，要在 stack 上量一次；1020 的獨立訊息與 Records 頁重送前重新讀取。記錄但不修兩項。CSV 多行引號欄位的行號指向該筆記錄的最後一行，已寫進 architecture §5.3。frontend 的 5 秒逾時早於 backend 的 10 秒 `read_timeout`，使用者可能在慢查詢時重送。

**M04 與 M05 駁回。** 沒有。三個模型提出的 43 項裡，沒有一項被駁回。

**M03 駁回。** 兩項。poison batch 的 bisect 與隔離，理由是 ADR-001 已決定永久錯誤讓 writer 停下等人處理，並把隔離列為 open item。分類修好之後，只有真的 poison 才會走到這條路。分頁關閉造成的孤兒 receiver，理由是 ADR-003 明確接受並用 `WS_MAX_CONNECTIONS` 限制。

### 6.4 審查的價值與限制

審查找到的東西，owner 自己的 mutation check 與驗證沒有找到。M02 的 Argon2 阻塞與 M03 的 commit timeout 都是例子。M03 的 timeout 項是 fable 指出：asyncmy 的 `read_timeout` 是 None，被黑洞的連線會讓 writer 停在 IN_FLIGHT 13 到 30 分鐘以上，而 `test_health.py` 還有一個測試把「buffer 滿加 IN_FLIGHT」釘成 HEALTHY。

M04 與 M05 的審查找到一個 critical，是匯出的公式注入與控制字元造成的 500。owner 的 13 個與 18 個 mutation 都沒有碰到這一項，原因是 mutation 只改壞已存在的規則，而這項問題是缺了一條規則（推論）。

限制有兩個。第一，M04 與 M05 在第一次驗收之後才送審，所以驗收表要重跑一次（第 7.2 節）。審查早一點做，就不會有兩次驗收。第二，三個模型都沒有瀏覽器，所以 UI 的實際外觀沒有被任何人審過。

## 7. 驗收結果

以下是第一次驗收。驗收者是 sonnet，在 `2798239` 的 detached worktree 上跑。第二次驗收在 `11c00dc` 上重跑，見 7.2。它在乾淨 volume 上起一個 compose stack，`.env` 由 `.env.example` 產生並換上新的 JWT secret。原始輸出與腳本在 `accept-scratch/`。

| ID | 狀態 | 證據摘要 |
|---|---|---|
| DEP-01 | PASS | `up --build -d --wait` exit 0。ready 200、`/docs` 200、三個 token 都是 200。 |
| DEP-02 | PASS | `down` 再 `up`，record 137 仍回 200，volume 保留。 |
| DEP-03 | PASS | `down -v` 後重建，`traffic_test` 存在。舊 volume 的那一半沒跑。 |
| AUTH-01 | PASS | viewer 寫入 403，筆數不變。 |
| AUTH-02 | PASS | user 改或刪 admin 的 record 回 404。 |
| AUTH-03 | PASS | register 帶 `role=ADMIN` 回 422。不帶 role 回 201 且角色是 USER。 |
| AUTH-04 | PASS | 無 token 以 4401 關閉。有 token 的第一則是 status。 |
| TZ-01 | PASS | `pytest -k timezone` 2 passed。`+08:00` 寫入讀回 `08:00:04Z`，DB 內是 `08:00:04.000000`。 |
| HEALTH-01 | PASS | 停 DB 時 `/health/ready` 回 503 DEGRADED，`total_uncommitted` 45 升到 141，`/admin/system` 回 503。 |
| REC-01 | PASS | 總數 99 等於 33 加 33 加 33。分頁不重疊。遞增遞減排序成立。`page_size=101` 回 422。 |
| BUF-01 | PASS | 307 個樣本，`queued + in_flight + retry_pending == total_uncommitted` 違反 0 次，最大值 141。 |
| BUF-02 | PASS | `last_commit_at` 有 42 個不同值，停機以外 `total_uncommitted` 沒超過 15。 |
| RETRY-01 | PASS | 停 DB 約 31 秒。停機後 35 秒回到 HEALTHY。新增 153 筆等於 51 秒乘每秒 3 筆。資料列 888/888 不重複。WS client 收到 211 則事件。 |
| WS-01 | PASS | 三次都是事件先從 WS 收到，+0.01 秒時資料庫查不到，+6.5 秒時查得到。 |
| WS-02 | PASS | 前端 unittest 11 OK。Live 頁連續 rerun 20 次，`active_connections` 都是 1。 |
| IMP-01 | PASS | CSV 15 列匯入成功。value 為 -1 回 422 並指出第 4 列。未知 service 回 422 指出第 5 列。筆數不變。重複 id 回 409。 |
| ANA-01 | PASS | payment 的 summary count、records total 都是 313。categories 加總 941 等於 summary。 |
| ANA-02 | PASS | xlsx 可用 openpyxl 開，314 列，資料列 313 等於 count。 |
| ADM-01 | PASS | viewer 升為 USER 後舊 token 可寫入，降回後 403。自我停用與降級回 422。logs 有 2 筆 `user.updated`。 |
| CI-01 | NOT RUN | 在 GitHub 上、push 之後才會跑。之後通過，見第 1 節。 |

### 7.1 要誠實說的幾點

**CI-01 沒有執行。** workflow 在 `.github/workflows/ci.yml`，第一次驗收時只在本機跑過其中的 ruff 命令。之後每個 run step 都在本機重放過（第 7.3 節），但 GitHub Actions 本身還沒有執行。push 之後的第一次 run 通過（第 1 節）。

**WS-02 有一個 flake。** 第一次（冷啟動）執行時，`AppTest` 在 10 秒逾時，11 個測試花了 25.9 秒。之後兩次 rerun 在 0.6 到 0.7 秒內通過，另外 20 次 live rerun 也都成立。驗收者把它記為 cold-start flake，沒有重現。README 也記下這件事，逾時之後已改為 30 秒，冷啟動執行沒有再驗證。M05 owner 在主機上第一次跑前端頁面測試時也看過同一個現象（第 5.5 節），兩份紀錄互相呼應，但原因都沒有被證實。

**DEP-03 只驗了一半。** 舊 volume 缺 `traffic_test` 的那一半沒有跑。這一半之後由 DEP-03b 演練補上（第 7.4 節）。

**其他只做到一部分的案例。** AUTH-04 的 token 過期 → 4401 只由單元測試覆蓋，live 只驗了無 token 的 4401 與 `status`。BUF-02 的 live 驗證只看到 5 秒觸發，100 筆觸發由單元測試覆蓋。WS-01 以時間差驗證（WS 事件先收到、DB 約 6.5 秒後才看得到），Writer 沒有被暫停。DEP-01 的登入走 `/auth/token` API，不是瀏覽器 UI。

**ADM-01 的「最後一個 admin」規則沒有在驗收中跑。** API 無法指定另一個 admin 為目標，所以驗收者用自我降級來走這條規則。兩個 admin 的競爭由 integration test 驗證，驗收沒有重跑。

**RETRY-01 有兩次執行。** 第一次的 WS client 輸出因為 shell 的 noclobber 錯誤而遺失，所以沒有拿來當 WS 證據。第二次是正式證據。

**IMP-01 的說明。** seed 已經匯入 JSON sample。CSV 的前 13 列沒有 event_id，所以再匯入一次會因內容重複而成功。第 14 到 16 列有固定 id，是驗收者第二次匯入同一個 CSV 才發現。

**沒有人用瀏覽器看過 UI。** 所有前端驗證都用 Streamlit `AppTest`，它不會畫出畫面，也不會觸發 `st.fragment(run_every=1)` 的計時器。這是擁有者的規則，不是疏忽。

**驗收表有 20 列，不是 19 列。** 19 個案例加 CI-01。README 的狀態表與 testing.md 的 ID 一一對應。

**MANUAL 項目。** 這份驗收沒有任何被標成 MANUAL 的列。testing.md 的「手動 / 故障」清單中沒有執行的項目是兩個：`docker compose kill api` 後的遺失邊界展示，以及乾淨 clone 後在瀏覽器以三個帳號走完所有頁面。

### 7.2 第二次驗收（`11c00dc`）

**為什麼重跑。** 修正輪在第一次驗收之後改了匯入、匯出、管理與 schema 的程式，驗收證據必須對應交付的 commit。coordinator 在 06:11 啟動，結果在 06:20，合併為 `ca9e2c6`。

**結果。** 19 個案例 PASS，CI-01 NOT RUN（之後在 GitHub 上通過，第 1 節），0 FAIL。流程與第一次相同，在 detached worktree 上用單一 compose stack 照 testing.md 的表依序執行，結束時 `down -v` exit 0，沒有留下容器或 volume。

**新增的檢查。** 匯入第一個 title 是 `=HYPERLINK("http://evil","x")` 的檔案回 201，存下的 title 等於那段文字。匯出的 xlsx 的 sheet 沒有 `<f>` 元素，那個儲存格的 data type 是 `s`，是文字。title 含 `\x01` 的匯入回 422 並指出第 3 列，筆數不變。6 MB 的上傳回 413 `PAYLOAD_TOO_LARGE`。

**數字的變化。** RETRY-01 停 DB 31 秒（22:14:47 停、22:15:18 啟），停機後 58 秒回 HEALTHY。之前 `count(*)` 與 `count(distinct event_id)` 皆 263，之後皆 494。WS client 在 75 秒內收到 226 則不重複事件，沒有超過 5 秒的間隔。BUF-01 取了 255 個樣本，恆等式違反 0 次，最大 `total_uncommitted` 是 180。BUF-02 看到 35 個不同的 `last_commit_at`。WS-02 是 17 個前端測試。

**這一輪沒有做的事。** `observed_at` 的 epoch 數字被拒絕，只由單元測試覆蓋，驗收腳本在那一步之前中止。AUTH-04 的 token 過期與 BUF-02 的 100 筆觸發仍只由單元測試覆蓋。WS-02 沒有重做真實 api 上的 20 次 rerun。IMP-01 的第一次嘗試回 409，因為檔案的第 14 到 16 列帶有固定 `event_id`，驗收者把這三個 id 清空後才匯入成功，這與第一次驗收的發現一致。DEP-03b 沒有重跑，證據仍是 `e2bd097`。

### 7.3 CI 步驟重放

CI-01 要等 GitHub 上的 Actions 執行，所以 coordinator 在 05:11 驗證了一個替代做法。`replay.py` 解析 `ci.yml`，在本機依序執行每個 run step。所有步驟都 exit 0，包含 `ruff check`、`ruff format --check`、frontend 的 `unittest`、建立 `.env`、`docker compose up --build -d`、等待 `/health/ready`、api 容器內的 `pytest -q` 與 `down -v`。這次重放發生在修正輪之前（commit `8d7d1d6`），pytest 是 339 passed。

重放與真實 runner 的差別有幾處。`actions/checkout` 用現成的 worktree，`setup-uv` 用 PATH 上的 uv 而沒有檢查版本，Docker 是 Podman 上的 docker CLI，Python 3.12 由 uv 提供。所以 CI-01 當時仍是 NOT RUN。剩下的風險只在 runner 環境；push 後的真實 run 通過（第 1 節）。

### 7.4 DEP-03b 演練

第一次驗收沒有驗到「既有 volume 沒有 `traffic_test`」這一半，coordinator 在 05:03 派出演練（見 8.5），05:06 驗證並合併為 `a40a368`。

結果是 PASS。用一個不掛載 `db/init` 的 override 在 `db-data` 上啟動，`SHOW DATABASES` 只有 `traffic`。同一個 volume 上換回正常的 stack，`traffic_test` 仍然不存在，`pytest` 在 setup 階段以 `1044 Access denied` 失敗。`down -v` 之後重新啟動，`traffic_test` 存在，`/health/ready` 回 200，16 個 health 測試通過。這證明已有 volume 的環境要 `docker compose down -v` 重建，README 也這樣寫。

## 8. 砍掉、延後、未驗證

### 8.1 計畫中就決定砍掉的範圍

來自 plan.md 的「砍掉的範圍」。

- barrier 式 WS 測試的自動化、完整故障演練的自動化、MariaDB 相容性矩陣。
- 同 `event_id` 不同內容的偵測，永久錯誤批次的 durable quarantine。
- refresh token、revocation、rate limit、DB 故障期間的 admin 存取。
- 多實例、broker、replay。
- Admin 頁只做必要功能。

### 8.2 實作中留下的 follow-up

`followups.md` 的項目，其中標明 README 已知限制的已寫進 README。

- 登入吞吐量受 CPU 限制（每次 Argon2 verify 約 28 ms），rate limit 是 ADR-002 的 open item。
- `IMPORT_MAX_BYTES` 在 Starlette 已緩衝整個上傳之後才檢查。修正輪加了 `Content-Length` 的 413 檢查，但 chunked 上傳要擋在邊緣仍需要 reverse proxy。
- 已解決。`import_batches` 的 commit 在資料 commit 之後失敗的空檔，由修正輪的單一 transaction（`90c0d01`）關閉。
- Streamlit 側欄對所有角色都列出 Admin 頁。非 admin 只看到提示，API 一律 403。隱藏它要把 `app.py` 改成 `st.navigation`。
- 已解決。`system_logs` 的索引由 migration `0002`（`1f20db7`）加入。
- 1020 snapshot conflict 回 503「Database unavailable」，409 會更精確。
- `/admin/system` 每次對五張表 `count(*)`，資料量到數百萬列時可能接近 10 秒的 `read_timeout`。修正輪加了 10 秒快取，降低重複次數，沒有降低單次成本。
- Admin 的 `PATCH` 會鎖整張 `users` 表，持續時間是毫秒等級。migration `0002` 加了 `users (role, is_active)` 的索引，這項是否因此縮小鎖定範圍，沒有量測。
- asyncmy 的 `read_timeout` 等於 `COMMIT_TIMEOUT_SECONDS`（10 秒），對共用 engine 上的每個查詢都有效。
- 健康檢查用 `CMD-SHELL`，沒有在 Docker Engine 上驗證。
- compose 的 api image 含 dev 依賴（`INSTALL_DEV=true`）。

### 8.3 仍然沒有被證明的主張

- **Docker Engine 上的行為。** 所有執行都在 Podman 上。
- **乾淨 clone 照 README 跑一遍。** plan.md 的 M06 退出條件是這一項，沒有完成。
- **瀏覽器中的畫面與計時刷新。** 沒有任何人看過。
- **大資料量的行為。** `/admin/system` 與 `/admin/logs` 在百萬列時的表現是推論，沒有量測。M04 的效能數字只到 100k 列。
- **設計階段花了多久。** 沒有記錄。
- **兩個 admin 的競爭規則在 live stack 上的行為。** 只有 integration test。

### 8.4 證據的來源與限制

**coordinator 自己的驗證沒有 log 檔。** coordinator 在每個 milestone 合併前的 `compose up`、pytest 與 curl 驗證，只留在 session transcript 與 trail 裡。owner 的執行與驗收有檔案放在 `.audit/` 之下。

**mutation 次數是 owner 自己報的。** review log 與 trail 中的 mutation 次數來自實作的 agent，沒有被獨立重跑。

**row lock 的延後理由是錯的。** 當初以「單 worker」為由延後 row lock，獨立審查指出單 worker 仍會在 `await` 之間交錯：並行 DELETE 可能回 500，稽核的 before 值可能過期。這個限制現在寫在 README 的已知限制。

### 8.5 stop-hook 事件

第一次交付後，coordinator 在 05:01 寫下「run complete」。05:03，Claude Code 的 stop hook 檢查列出尚未完成的事項。hook 要求處理的是 CI-01 NOT RUN、DEP-03 只驗一半、沒有人用瀏覽器走過畫面、`how` 與 `architect` 被略過、以及文件需要擁有者確認。

這幾項裡，當下可以在本機關閉的有兩項。DEP-03b 派出演練後在 05:06 證明（第 7.4 節）。CI 的 run step 在 05:11 重放完成（第 7.3 節）。檢查仍有未關閉的項目時，coordinator 又派出 M04 與 M05 的審查（第 5.8 節）。

只有擁有者能做的有三項。CI-01 需要擁有者 push 到 GitHub，瀏覽器走查是擁有者自己定的規則，README 中「開發方式」那一段的第一人稱是只有作者能確認的。`how` 與 `architect` 是依 playbook 的「略過並寫理由」規則略過，因為 ADR 已固定設計，決策紀錄記下理由，沒有補做。

## 9. 學到的事

每一條都對應到紀錄裡的一個時刻。

1. **讓演練的順序等於保證的順序。** M03 的第一次停 DB 演練在 02:02 連線，在 DB 停掉之後，驗的是新連線，不是 ADR-003 保證的既有連線。coordinator 當天就發現並寫進決策紀錄。下一次寫演練腳本時，先寫下「這次要證明哪一條保證」，再決定連線與停機的先後。
2. **mutation check 找的是「測試根本沒測」的地方。** M02 的遞增 id 次排序 mutation 讓 0 個測試失敗。M03 第一輪有兩個 mutation 沒被抓到，才催生了 `293c3af`。把 mutation 清單與測試一起交付，比只交付綠色的測試有用。
3. **三個模型一致才算強訊號，單一模型要附執行證據。** M03 的 errno 分類與關閉競爭都是單一模型提出，但分別被執行證明，所以被採納。sonnet 的 poison quarantine 沒有執行證據，且與 ADR 衝突，所以被駁回。這條規則讓合成審查不必靠投票。
4. **ADR 要先於 reviewer。** M03 的兩項駁回都靠「ADR 已決定」一句話解決。沒有 ADR，這兩項會變成需要討論的設計問題。
5. **brief 說清楚目標，owner 才有偏離的空間。** M02 修正輪的第 14 項，owner 發現照字面做會產生循環 import，改成反方向，達成同一目標。coordinator 接受。如果 brief 只給步驟，這個偏離會變成錯誤。
6. **整個 demo 流程要在計畫階段對一次。** M04 發現「匯入 sample CSV 得 201」與 §5.3 的 409 規則在已 seed 的環境互相矛盾。這個缺口到 M05 才用「seed 改用 JSON」關閉，中間多了一輪。把 plan.md 的展示流程當作一個測試腳本，在 M01 就跑一次紙上推演，可以更早發現。
7. **用真的資料庫才找得到 1020。** M05 的 last-admin 測試在真實 MariaDB 上得到 1020，原因是 11.7 預設 `innodb_snapshot_isolation=ON`。mock 不會產生這個結果。CLAUDE.md 的規則 4 與 5（真實 MariaDB 的 integration test、mock 通過不算）在這一刻回本。
8. **從紀錄複製數字，不要手打。** plan.md 第 5 行寫了 commit message 描述驗證，但 commit 其實只有 subject，M06 owner 才發現。下一次，brief 裡的數字要從 `decisions.tsv` 用腳本產生。
9. **在做的時候記錄，不要事後補。** 決策紀錄開頭多列的時間戳都擠在 23:37 到 23:39（含一列 `pending` 與一列取代它的列），這代表它們是批次寫入的，不是事件發生時寫的（推論）。設計文件的時間完全沒有記錄，所以最重要的前置工作無法計時。
10. **獨立驗收者會看到 owner 與 coordinator 都沒看到的東西。** 驗收者是唯一重跑同一份 CSV 而發現第 14 到 16 列的固定 id。它也把第一次 RETRY-01 的遺失輸出標成「不採用」，而不是拿來湊證據。獨立性來自它沒有參與寫程式。
11. **要誠實記錄沒做的事。** 瀏覽器、Docker Engine、CI、乾淨 clone 都沒做，而且 README 的已知限制與 testing.md 都寫了。這讓交付時的說法與實際相符。
12. **每個 milestone 都要在驗收之前審查，不是之後。** M04 與 M05 在第一次驗收與文件交付之後才審查，找到匯出的公式注入這個 critical，修正又改了匯入、匯出、管理與 schema 的程式，所以驗收要重跑，文件的數字要全部更新。mutation 檢查沒有抓到它，因為那是缺少的規則，不是被改壞的規則。
13. **新的保護規則可能讓舊規則走不到，mutation 會顯示出來。** 操作者新鮮度檢查加上「不能動自己」之後，「最後一個有效 ADMIN」的程式在 API 上無法觸發，拿掉它的 mutation 存活。owner 沒有自行刪掉，而是回報，coordinator 依 ADR-002 決定保留。存活的 mutation 是一個要人決定的訊號，不一定是測試的缺口。

## 10. 附錄

### 10.1 `.audit/` 資料夾

| 檔案或資料夾 | 用途 |
|---|---|
| `decisions.tsv` | 決策紀錄，一個決定一列，這份文件的時間線來源。 |
| `todo.md` | Feature playbook 的八個步驟，哪些略過與原因，以及 milestone 的勾選清單。 |
| `briefs/` | 每個 owner 收到的任務說明（m01 到 m05、兩個修正輪、accept、m06 三份）。 |
| `reports/` | 每個 owner 的 report，含預計變更範圍、驗證命令與 exit code、NOT RUN 與風險。 |
| `reviews/` | 每次 interrogate 的三份 reviewer 輸出與一份 synthesis（M02、M03、M04 與 M05），另有對決策紀錄本身的審查。 |
| `acceptance.md`、`acceptance-2.md` | 兩次獨立驗收的完整證據。 |
| `followups.md` | owner 回報、留給 README 與後續工作的項目。 |
| `evidence/` | 修正輪與 M04、M05 的驗證腳本與輸出。 |
| `accept-scratch/`、`accept2-scratch/` | 兩次驗收的暫存腳本與原始輸出。 |
| `*.done` | owner 完成的標記檔，供 coordinator 偵測。 |

### 10.2 commit 清單

以 `git log` 為準，依 milestone 分組。`main` 在 `babd5e4` 是 88 個 commit，另加本文件與更正兩個 commit。之後又有 28 個 commit，`main` 現在是 `ca9e2c6`，共 118 個。

#### 設計文件（base）（1）

- `6f395f9` docs: add architecture, ADRs, plan and testing spec

#### M01（8）

- `2fcf356` build: add backend and frontend uv projects with pinned dependencies
- `62b06ff` feat(db): add models with UTCDateTime and initial alembic migration
- `2e9298a` feat(api): add JSON logging, request id, error envelope and health endpoints
- `fa0b27e` feat(frontend): show project name and backend readiness
- `22199ca` build(compose): add db, api and frontend services with multi-stage images and healthchecks
- `80143f8` test: add TZ-01 round-trip, migration, health and error envelope tests
- `f3a4d62` ci: add ruff and compose pytest workflow
- `252d83a` docs: rewrite local run for uv and record M01 review

#### M02 owner（8）

- `50d5555` feat(auth): add Argon2id hashing, HS256 JWT and current-user dependencies
- `e1a7e20` feat(auth): add register, token and me endpoints with audit
- `75252ae` feat(startup): ensure services, bootstrap admin and seed demo users
- `15b4f5f` feat(services): list services and audit admin threshold changes
- `6cf28a0` feat(records): add CRUD, bulk create, list filters and ownership checks
- `54ea1cb` feat(api): mount auth, services and records routers under /api/v1
- `3b7c24b` test: cover role matrix, ownership, tokens, records and bootstrap on MariaDB
- `b8faf46` docs: record M02 review in ai-review-log

#### M02 修正輪（18）

- `ccd44a7` fix(auth): run Argon2 off the event loop and cap password length
- `41298ba` fix(auth): treat a malformed stored hash as a failed login
- `e167465` fix(api): map connection errors to 503
- `73b8c58` fix(api): send WWW-Authenticate: Bearer with every 401
- `77ecc55` fix(records): deny unknown roles in assert_can_modify
- `41a9495` fix(db): hide bound parameters in SQLAlchemy errors
- `49d8422` fix(startup): validate bootstrap and demo credentials with the register rules
- `152529c` fix(config): reject the placeholder JWT secret
- `767e89f` fix(auth): validate login emails through the shared Email type
- `7b4664d` fix(records): reject empty PATCH and audit every changed field
- `f3449de` fix(records): treat inactive services as unknown on create
- `85709f9` fix(records): bound list and create inputs at the schema
- `2ce23cd` refactor(api): rename the Writer role dependency to WriterRole
- `759687e` fix(records): rename the created_at sort key to persisted_at
- `dd79a9e` docs: record the persisted_at sort key decision in R-005
- `7b44eb3` test: pin token claims, algorithms, conflict mapping, register audit and bulk statuses
- `bb093fc` docs(architecture): document the 1,000-item bulk limit
- `964ea67` test(records): prove sort=persisted_at orders by that column

#### M03 owner（13）

- `a133e78` test(ingestion): specify buffer capacity, flush and retry invariants
- `4800183` feat(ingestion): add bounded buffer with reservation-based capacity
- `7b39997` feat(ingestion): add batch writer with backoff retry and duplicate-safe insert
- `293c3af` test(ingestion): cancel accept() at each await point
- `b80a68e` test(ingestion): prove batch insert and duplicate recovery on MariaDB
- `bc27b4e` feat(realtime): add simulator with threshold reload and accept-before-broadcast
- `a088283` feat(realtime): add WebSocket connection manager with per-connection queues
- `c8463f2` feat(api): serve WS /api/v1/ws/traffic with handshake auth and pipeline lifespan
- `47c8fe5` feat(health): report real ingestion counters and the readiness state machine
- `11652e2` feat(frontend): add WebSocket receiver thread, auth client and session helpers
- `bedff7c` feat(frontend): add login page with logout and expiry notice
- `652fcc6` feat(frontend): add Live Monitor page with live charts and connection state
- `7d8916f` docs: record M03 review in ai-review-log

#### M03 修正輪（17）

- `0de861a` test: scope the pipeline fixture to the client session and fail on leaked overrides
- `667e4a2` fix(ingestion): classify writer errors by errno and invalidated connection
- `e1ccc37` refactor(ingestion): let session.begin() roll back and select missing rows in one helper
- `8b666a0` fix(ingestion): assert the uncommitted count never exceeds capacity instead of truncating
- `8a6c2d2` fix(health): stay DEGRADED from a batch's first failure until it commits
- `e2d732e` fix(ingestion): bound each commit attempt and report a stalled full buffer as UNAVAILABLE
- `e4e7dba` fix(ingestion): cut the back-off short on close, make one last attempt, and never strand a raced event
- `64a3742` fix(realtime): stop the simulator within one tick once the writer halts
- `21874c9` fix(realtime): keep the last threshold map on any reload error and dispose the engine after a failed startup
- `ff1f2b4` test(realtime): push status changes through an injectable poll and prove each change is pushed once
- `e004c27` fix(ingestion): report writer counters where the events are, not by subtraction
- `1741b99` fix(realtime): close an overflowing or stalled client from a cleanup task and bound every send
- `00e5b22` fix(realtime): close an idle socket with 4401 when its token expires
- `0ef71d2` fix(api): check the WebSocket cap before the user reload and document 1013 for a database outage
- `d915d41` fix(frontend): keep the receiver alive on any error, jitter its back-off, and replace a crashed one
- `0518d84` docs: note that uvicorn closes sockets with 1012 before the lifespan flush
- `6fd52c2` docs(ingestion): note that _persist also gives up after its last attempt at shutdown

#### M04（10）

- `92685e1` feat(imports): add all-or-nothing CSV / JSON import with row errors and a batch record
- `dea176f` feat(analytics): add summary, categories and trends aggregated in MariaDB over the records filters
- `1c1e46e` feat(analytics): export the filtered records as xlsx, capped at EXPORT_MAX_ROWS
- `6f9febc` feat(bootstrap): import the sample CSV as the demo user when no imported rows exist
- `1bc8f34` feat(frontend): add Records page with filters, role-aware create / edit / delete and file import
- `d40c24c` feat(frontend): add Analytics page with KPI tiles, per-service table, trend chart and Excel download
- `b0b1825` test: cover import parsing, all-or-nothing import, analytics values and the xlsx export
- `9ffd36d` fix: resolve data/ beside the backend so local and container runs find the sample files
- `e04ec01` docs(imports): word the batch-commit gap comment plainly
- `2e351e7` docs: record the M04 import and analytics contract and the R-007 review

#### M05（10）

- `ee28d75` fix(api): answer 503 only for connection-class database errors and log the errno
- `584ad91` refactor(audit): name every system_logs event type in one AuditEvent enum
- `45fa7a8` feat(admin): list users and change role or activation under the ADR-002 protective rules
- `5fc43a2` feat(admin): add filtered system logs, their event types and the system status endpoint
- `a0bbecf` fix(bootstrap): seed the demo records from the JSON sample so the CSV sample still imports
- `87148ab` feat(frontend): add the Admin page for users, thresholds, system status, logs and history
- `c25f714` test: cover the admin API, the last-admin race, the Admin page and receiver reuse across reruns
- `5a6f90f` ci: run the frontend unit tests on the runner
- `60eac2d` fix(api): treat a MariaDB snapshot conflict (1020) as retryable like a deadlock
- `2798239` docs: record the M05 admin contract, the errno classification and the R-008 review

#### M06 文件（3）

- `4f1ebb5` docs(readme): fill delivery status, development process, known limits and local run
- `9b350e2` docs(testing): record acceptance evidence for 19 cases and leave CI-01 not run
- `babd5e4` docs: align .env.example with Settings, add review-log findings and plan results

#### 本文件與更正

- `4cbed55` docs: correct the commit-message note and the timeline label

#### 誠實性修正（10）

- `f768158` docs(readme): state that nothing ran on Docker Engine or GitHub Actions
- `99fe2b3` docs(readme): add the JWT secret step to the quick start
- `587c302` docs(readme): align the delivery status table with the testing IDs and partial evidence
- `7b01915` docs(testing): make the evidence column say which half of a case ran
- `99a1eeb` docs(readme): name the AI coordinator as the per-milestone reviewer
- `66078d3` docs(readme): list the concurrency, outage and crash-loss limits
- `b3f4e55` docs: record M06 as merged in the plan results
- `27f1b83` docs: add evidence provenance and partial-case notes to the process record
- `f33ced1` docs: drop the stray follow-up wording from the M06 plan row
- `e2bd097` test(frontend): raise the AppTest timeout to 30 seconds

#### DEP-03b 與 CI 重放的證據（2）

- `a40a368` docs(testing): record the existing-volume half of DEP-03
- `8d7d1d6` docs(testing): record the local replay of the CI steps

#### M04 與 M05 審查後的修正輪（15）

- `671ca02` fix(export): write text cells, reject control characters in titles and clean up a failed export
- `3b45cb4` fix(imports): reject nested JSON, long integers and empty files at file level, name masked duplicates and bound field names
- `90c0d01` fix(imports): commit an accepted file's batch, rows and audit row in one transaction
- `51b105c` fix(db): treat packet, read-only, interrupted-query and statement-timeout errnos as transient
- `1f20db7` feat(db): index system_logs, traffic_records.observed_at and the active-admin lookup
- `4844f04` perf(admin): cache the system table counts for 10 seconds in the api and on the Admin page
- `c784177` fix(admin): refuse a user change from an admin demoted after authentication and release no-op locks
- `2edd11c` fix(audit): refuse event types outside AuditEvent and type the level as one shared Literal
- `3b9c55d` fix(frontend): default the Analytics window to the last 24 hours and test the trend bucket cap
- `69aaad4` feat(imports): answer 413 before reading an upload whose Content-Length is over the limit
- `cdc410f` feat(writer): warn when a simulator event_id is already stored by another source
- `7791eba` fix(records): reject Unix-time numbers where an ISO 8601 instant is expected
- `2dc1d68` test(frontend): cover the Records and Analytics pages with AppTest
- `09e077b` test(imports): cover the 100-error cap and that an import never touches the buffer or the sockets
- `11c00dc` docs: record the CSV row numbering, the export and title rules and the M04/M05 multi-model review

#### 第二次驗收的證據（1）

- `ca9e2c6` docs(testing): refresh acceptance evidence after the review fixes
