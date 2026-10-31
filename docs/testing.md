# 測試計畫與驗收證據

## 執行方式

```bash
docker compose up -d db api
docker compose exec api pytest -q                   # 全部
docker compose exec api pytest tests/unit -q        # 單元
docker compose exec api pytest tests/api -q         # API / 權限
docker compose exec api pytest tests/integration -q # 真實 MariaDB
docker compose exec api ruff check . && docker compose exec api ruff format --check .
```

測試使用 `TEST_DATABASE_URL`（預設指向同一個 MariaDB 的 `traffic_test` 資料庫）。fixture 以 Alembic 建 schema，每個測試後 truncate。SQLite 不能代替 MariaDB：型別、unique、timezone round-trip、`IntegrityError` 行為都要在真的 MariaDB 上過。

## 層次與必測案例

### Unit（`tests/unit`）

- 驗證：`value` 非負整數；naive datetime → 422；threshold 79 / 80 / 81 → 正常 / 正常 / 異常。
- Ownership：{owner, admin, other user, NULL owner} × {ADMIN, USER, VIEWER}。
- Buffer（注入時鐘與 fake session）：100 筆觸發；5 秒觸發；空 queue 不 flush；容量含 in-flight（Writer 持批時 `accept()` 會等）；commit 後才 release；retry 保留 `event_id` 與 snapshot；backoff 上限 30 s；reserve 後 cancel 退回容量。
- Generator：固定 seed 輸出可重現；三個 Service 各一筆。
- Analytics：空集合的 sum / avg；半開區間邊界。
- Import parser：行號錯誤回報；檔內重複 `event_id`；未知 Service。

### API / Security（`tests/api`，httpx `AsyncClient`）

以 ADMIN、USER A、USER B、VIEWER 四個身分：

- 無 token / 過期 / 錯簽章 / `alg=none` → 401。
- register 帶 `role=ADMIN` → 422；成功 register 為 USER。
- VIEWER 的 POST / PATCH / DELETE / bulk / imports → 403。
- USER A 改刪 USER B 的 record → 404；改刪 `created_by=NULL` → 404。
- request 帶 `source` / `created_by` / `is_anomaly` → 422。
- ADMIN 改 threshold / 改角色 → 200 且 `system_logs` 有紀錄。
- Admin 停用自己 / 停用最後一個 ADMIN → 422。API 上最後一個有效 ADMIN 只會是操作者自己，由「停用自己」擋下；「最後一個 ADMIN」規則在兩個 ADMIN 並行互相降級時生效，用 integration test 直接呼叫 service 驗證。
- 分頁 total 正確、排序穩定、`page_size=101` → 422。
- 所有成功 response 不含 `password_hash`。
- WebSocket 無 token → close 4401；有效 token → 收到 `status` 訊息。

### Integration（`tests/integration`）

- Alembic upgrade head / downgrade base 在空 DB 成功。
- Writer 真寫 MariaDB：批次 insert；先手動插入其中一筆再 flush → 只補缺的、無重複、整批 release。
- Import All-or-Nothing：中間一列錯 → `traffic_records` 筆數不變、`import_batches` 記 FAILED 並含行號。
- `UTCDateTime` round-trip：寫入 `2026-10-31T16:00:04+08:00` → DB 存 naive `2026-10-31 08:00:04.000000` → 讀出 aware UTC 等值；naive datetime 進到 type 層 raise。

### 手動 / 故障（結果記在下表）

- `docker compose stop db` 30 秒再 `start`：Live 持續、`/health/ready` 顯示 DEGRADED、恢復後 drain、`select count(*)` 與 `count(distinct event_id)` 相等。
- `docker compose kill api` 再 `up`：DB 只有已 commit 的資料，展示遺失邊界。
- `docker compose stop db` 期間：`GET /health/ready` 不帶 token 回 503 但 body 完整，`state=DEGRADED`、`total_uncommitted` 遞增；`GET /admin/system` 回 503（auth 依賴 DB）。
- Streamlit 連續 rerun 20 次：`/admin/system` 的 `active_connections` 維持 1。
- 乾淨 clone → 照 README 啟動 → 三個帳號走完全部頁面。

## 驗收證據

| ID | 對應需求 | 方法與預期 | 狀態 | 證據 |
|---|---|---|---|---|
| DEP-01 | 一鍵啟動 | 乾淨 clone → `docker compose up --build` → ready 200、`/docs`、UI 登入 | PASS | `docker compose up --build -d --wait`（exit 0）；ready 200、`/docs` 200、三個帳號 `/auth/token` 與 `/auth/me` 皆 200，角色為 ADMIN / USER / VIEWER。登入以 API 驗證，沒有在瀏覽器操作 UI；驗收在 worktree 內執行，不是乾淨 clone |
| DEP-02 | Volume 持久化 | `down` 後 `up` 資料仍在 | PASS | `docker compose down` 再 `up -d --wait`；volume `db-data` 仍在，`GET /records/137` 回 200，`observed_at` 為 `2026-10-31T08:00:04Z`（down / up 的 exit code 經 `tail` 讀取，未單獨記錄，以輸出為證） |
| DEP-03a | 測試庫初始化（全新 volume） | 全新 volume：`traffic_test` 存在 | PASS | `docker compose down -v`（exit 0）、`up --build -d --wait`（exit 0）；`SHOW DATABASES` 列出 `traffic_test`，ready 200 |
| DEP-03b | 測試庫初始化（既有 volume） | 既有 volume：`traffic_test` 不存在，`down -v` 後存在（initdb 只跑一次） | NOT RUN | 沒有在既有 volume 上執行 |
| AUTH-01 | VIEWER 不可寫 | `POST /records` → 403，DB 不變 | PASS | viewer `POST /api/v1/records` 回 403 `FORBIDDEN`，筆數前後都是 99 |
| AUTH-02 | Ownership | USER A 改 USER B 的 record → 404 | PASS | admin 建立 record 後，USER `PATCH` 與 `DELETE` 皆回 404 |
| AUTH-03 | 不可自行升權 | register `role=ADMIN` → 422 | PASS | register 帶 `role=ADMIN` 回 422；不帶 role 回 201、`role=USER`、body 無 `password_hash` |
| AUTH-04 | WS 認證 | 無 token 連 WS → 4401 | PASS | api 容器內的 `websockets` client：無 token 收到 close 4401（exit 0）；有效 token 第一則是 `status`，之後 10 則事件皆不重複（exit 0）。token 過期 → 4401 只由單元測試覆蓋，沒有在 live 驗證 |
| TZ-01 | 時間 round-trip | `2026-10-31T16:00:04+08:00` 寫入，讀出 aware 且與寫入比較相等、`utcoffset()==0`；DB 內為 naive `08:00:04` | PASS | `docker compose exec -T api pytest -q -k timezone` exit 0，2 passed；API 寫入 `2026-10-31T16:00:04+08:00`，讀回 `2026-10-31T08:00:04Z`；DB 內為 `2026-10-31 08:00:04.000000` |
| HEALTH-01 | DB 故障可觀察 | 停 DB 時 `/health/ready` 無需認證回 503，body 為 `state=DEGRADED` 與計數 | PASS | `docker compose stop db` 後 10 秒，不帶 token 的 `/health/ready` 回 503，`state=DEGRADED`、`writer_state=RETRY_WAIT`，`total_uncommitted` 45 並上升到 141；admin token 的 `/admin/system` 回 503 |
| REC-01 | 分頁 / 篩選 / 排序 | total 正確、排序穩定、filter 生效 | PASS | 總數 99 = auth 33 + orders 33 + payment 33；新增 3 筆後 total +3；`page_size=2` 兩頁不重疊；`sort=value` 與 `sort=-value` 單調；`is_anomaly` true 7 + false 92 = 99；`page_size=101` 回 422 |
| BUF-01 | 容量含 in-flight | Writer 持批時總 reservation ≤ 1,000 | PASS | 整段驗收每秒取樣 `/health/ready` 307 次、0 個錯誤；`queued + in_flight + retry_pending != total_uncommitted` 為 0 次；最大 `total_uncommitted` 141（DB 停機期間），超過 1,000 為 0 次 |
| BUF-02 | Flush 條件 | 100 筆或 5 秒先到者觸發 | PASS | 只在 live 觀察 5 秒觸發；100 筆觸發只由單元測試覆蓋。每 2 秒讀 `/health/ready`：`last_commit_at` 在 6 秒內推進；整段共 42 個不同值；非故障期間 `total_uncommitted` 不超過 15 |
| RETRY-01 | DB 恢復 | 停 DB 30 s，恢復後 accepted = persisted，無重複 | PASS | 兩次停 DB 30 秒以上。完整的一次：停 04:37:04、啟 04:37:35，35 秒後回 HEALTHY；之後 `count(*)` 與 `count(distinct event_id)` 皆 888；多寫 153 筆 = 51 秒 × 每秒 3 筆；WS client 70 秒內收到 211 則、211 個不同事件、無超過 5 秒的間隔。另一次因 shell 錯誤沒有保留 WS client 輸出，不當作 WS 證據 |
| WS-01 | Real-time First | client 取得第一則事件後立刻查 DB，應查不到；約 6.5 秒後查得到（沒有暫停 Writer，以時間差證明 WS 早於 commit） | PASS | 以時間差驗證：client 取得第一則 `traffic.observed` 後立刻查 records，三次都是 +0.01 秒查不到、+6.5 秒查得到。Writer 沒有被暫停，所以這證明 WS 事件早於 DB 可見，不是 Writer 暫停時的行為 |
| WS-02 | Receiver 唯一 | rerun 後連線數維持 1 | PASS | `cd frontend && uv run --frozen python -m unittest` exit 0，11 tests OK（含 `test_reruns_keep_one_receiver_per_session`）；真實 api 上以 AppTest 跑 Live 頁 20 次 rerun，`active_connections` 維持 1，`stop()` 後為 0。第一次冷啟動執行曾因 AppTest 10 秒逾時失敗一次（11 tests，25.9 秒），之後三次重跑皆通過（0.6 到 0.7 秒），推測是冷啟動，未證實。不是真瀏覽器 |
| IMP-01 | All-or-Nothing | 錯一列 → 0 筆寫入、batch FAILED | PASS | `data/sample_traffic.csv` 匯入回 201、15 / 15 成功；第 4 列 `value=-1` 回 422 並指出第 4 列，筆數不變，`GET /imports/3` 為 FAILED；第 5 列未知 service 回 422；同批重匯回 409，列出第 14 到 16 列 |
| ANA-01 | 聚合正確 | 固定資料對帳 summary / categories / trends | PASS | 固定 `end=2026-10-31T20:38:00Z`：payment 的 summary count 313 = `/records` total 313；categories 加總 313；不帶 service 時 summary 941 = categories 加總 941；minute trend 9 個 bucket，無空 bucket，加總 313 |
| ANA-02 | Excel | 下載檔案可開、列數與 filter 一致 | PASS | `/analytics/export?service=payment` 回 200，xlsx 28,380 bytes；openpyxl 3.1.5 開啟，314 列，扣標頭 = 313 = count |
| ADM-01 | Admin 管理 | 改角色生效於下一個 request；停用自己 / 最後 ADMIN → 422 | PASS | viewer 改為 USER 後，舊 token `POST /records` 從 403 變 201，改回後 403；停用或降級自己回 422；`/admin/logs?event_type=user.updated` total 2；`/admin/system` 含 `pool` 與 `table_counts`。live 只驗證停用或降級自己 422（唯一的 ADMIN 只能以自己為對象）；兩個 ADMIN 並行互相降級的規則沒有在驗收執行，只由 integration test 覆蓋 |

環境：macOS，Podman 5.7 經由 docker CLI 執行 `docker compose`，MariaDB 11.7。所有 PASS 的 commit 是 `2798239`（M05 合併點），日期 2026-11-01（stack 時鐘 2026-10-31 UTC）。Docker Engine 沒有在本機測過。測試數量：後端 339 個（在 api 容器內跑 pytest），前端 11 個（主機上跑 unittest）。

驗收結果：18 個案例完整 PASS，DEP-03 只有 a（全新 volume）PASS、b（既有 volume）NOT RUN，0 個 FAIL，另有 CI-01 尚未執行（GitHub Actions 在 push 之後才會跑，workflow 是 `.github/workflows/ci.yml`）。

| ID | 狀態 | 備註 |
|---|---|---|
| CI-01 | NOT RUN | ruff 與 format 檢查只在本機跑過同一組 `uvx ruff@0.17.3` 指令（exit 0），pytest 與 docker build 要等 GitHub Actions |

狀態只有 PASS / FAIL / BLOCKED / NOT RUN。每個 PASS 附 commit、日期，以及命令與觀察結果；exit code 只在取得時記錄，部分命令經 `tail` 讀取。只做到一部分的案例，在證據欄寫明哪一半有跑。FAIL 修正後重跑該列。PASS 代表該環境該案例通過，不代表 production ready。原始輸出與驗收用的腳本保存在作者本機，沒有放進 repo，所以每列都在證據欄寫出證明它的觀察結果。所有驗證都在 Podman 5.7 經由 docker CLI 執行，Docker Engine 與 GitHub Actions 都沒有跑過。

「手動 / 故障」清單中沒有執行的項目：`docker compose kill api` 後的遺失邊界展示、乾淨 clone 後以三個帳號在瀏覽器走完全部頁面。
