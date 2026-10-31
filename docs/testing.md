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
- Admin 停用自己 / 停用最後一個 ADMIN → 422。
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
| DEP-01 | 一鍵啟動 | 乾淨 clone → `docker compose up --build` → ready 200、`/docs`、UI 登入 | NOT RUN | |
| DEP-02 | Volume 持久化 | `down` 後 `up` 資料仍在 | NOT RUN | |
| DEP-03 | 測試庫初始化 | 全新 volume：`traffic_test` 存在；既有 volume：不存在，`down -v` 後存在（initdb 只跑一次） | NOT RUN | |
| AUTH-01 | VIEWER 不可寫 | `POST /records` → 403，DB 不變 | NOT RUN | |
| AUTH-02 | Ownership | USER A 改 USER B 的 record → 404 | NOT RUN | |
| AUTH-03 | 不可自行升權 | register `role=ADMIN` → 422 | NOT RUN | |
| AUTH-04 | WS 認證 | 無 token 連 WS → 4401 | NOT RUN | |
| TZ-01 | 時間 round-trip | `2026-10-31T16:00:04+08:00` 寫入，讀出 aware 且與寫入比較相等、`utcoffset()==0`；DB 內為 naive `08:00:04` | NOT RUN | |
| HEALTH-01 | DB 故障可觀察 | 停 DB 時 `/health/ready` 無需認證回 503，body 為 `state=DEGRADED` 與計數 | NOT RUN | |
| REC-01 | 分頁 / 篩選 / 排序 | total 正確、排序穩定、filter 生效 | NOT RUN | |
| BUF-01 | 容量含 in-flight | Writer 持批時總 reservation ≤ 1,000 | NOT RUN | |
| BUF-02 | Flush 條件 | 100 筆或 5 秒先到者觸發 | NOT RUN | |
| RETRY-01 | DB 恢復 | 停 DB 30 s，恢復後 accepted = persisted，無重複 | NOT RUN | |
| WS-01 | Real-time First | 暫停 Writer，client 先收到、DB 尚無 | NOT RUN | |
| WS-02 | Receiver 唯一 | rerun 後連線數維持 1 | NOT RUN | |
| IMP-01 | All-or-Nothing | 錯一列 → 0 筆寫入、batch FAILED | NOT RUN | |
| ANA-01 | 聚合正確 | 固定資料對帳 summary / categories / trends | NOT RUN | |
| ANA-02 | Excel | 下載檔案可開、列數與 filter 一致 | NOT RUN | |
| ADM-01 | Admin 管理 | 改角色生效於下一個 request；停用最後 ADMIN → 422 | NOT RUN | |

狀態只有 PASS / FAIL / BLOCKED / NOT RUN。每個 PASS 附：commit、日期、命令、exit code、log 或截圖位置。FAIL 修正後重跑該列。PASS 代表該環境該案例通過，不代表 production ready。
