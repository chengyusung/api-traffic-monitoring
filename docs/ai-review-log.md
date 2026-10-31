# AI Review Log

記錄「AI 產出 → 審查 → 修正 → 驗證」的循環。規範在 [CLAUDE.md](../CLAUDE.md)，這裡是證據。

兩條規則：只記真的發生過的事；AI 第一版就正確時，記錄驗證了什麼，不編造錯誤。

每筆格式：

- **任務**：做什麼
- **AI 產出**：第一版的關鍵內容
- **審查**：檢查了什麼
- **發現與修正**：有問題就寫問題、原因、改成什麼、commit；沒問題寫「符合 ADR-xxx §n，無修正」
- **驗證**：命令、結果、exit code 或測試檔名

## 設計階段（實作前）

設計文件本身也是 AI 輔助產出的，下面三筆是對設計文件的審查結果，在寫程式之前就修正。

### R-001 時間欄位型別

- 任務：定義 `traffic_records` 等表的時間欄位。
- AI 產出：架構文件與 CLAUDE.md 初版指定 SQLAlchemy `DateTime(timezone=True)`。
- 審查：MariaDB 是否真的保存時區資訊。
- 發現與修正：MariaDB `DATETIME` 不存時區，`TIMESTAMP` 隨 session time zone 轉換；`timezone=True` 在 MariaDB dialect 上沒有實際效果，asyncmy 讀回 naive datetime。若照初版實作，`+08:00` 寫入後讀出會變成錯誤的時間。改為 `UTCDateTime` TypeDecorator（bind 轉 naive UTC、result 補 `tzinfo=UTC`、naive 輸入 raise）+ engine `init_command="SET time_zone='+00:00'"`。更新 architecture.md §5、CLAUDE.md。
- 驗證：testing.md TZ-01 round-trip；狀態 NOT RUN。

### R-002 測試資料庫不存在

- 任務：`.env.example` 與測試 fixture。
- AI 產出：`TEST_DATABASE_URL` 指向 `traffic_test`，compose 只設 `MARIADB_DATABASE=traffic`。
- 審查：MariaDB 官方 image 的初始化行為。
- 發現與修正：image 只建立 `MARIADB_DATABASE` 並對該庫授權，`traffic_test` 不會存在，測試第一次跑就會失敗。新增 `db/init/01-create-test-db.sh`（掛到 `/docker-entrypoint-initdb.d/`）建立 `${MARIADB_DATABASE}_test` 並授權給 `${MARIADB_USER}`。更新 architecture.md §2、§9、CLAUDE.md、`.env.example`。
- 驗證：`docker compose down -v && docker compose up -d db`，然後 `docker compose exec db mariadb -u traffic -p -e 'SHOW DATABASES'` 應列出 `traffic_test`；狀態 NOT RUN。

### R-003 DB 故障時看不到故障

- 任務：`/admin/system` 與 health 端點設計。
- AI 產出：ADR-002 決定每個 request 從 DB 重讀 user、DB 不可用時 503；ADR-001 把 buffer 指標只放在 `/admin/system`。
- 審查：兩個決策放在一起的後果。
- 發現與修正：DB 中斷時 `/admin/system` 本身 503，而那正是最需要看 queue 水位的時候。把 ingestion 計數（非敏感的營運數字）放進不需認證的 `/health/ready` body；`/admin/system` 保留 pool 與資料表統計。更新 architecture.md §9、ADR-001 Consequences、ADR-002 Consequences。
- 驗證：testing.md HEALTH-01；狀態 NOT RUN。

## 實作階段

每個 milestone 至少一筆。AI 第一版正確也要記驗證了什麼。

### R-004 M01 Foundation

- 任務：M01。uv 專案（backend / frontend 各一份 `uv.lock`）、compose（`db` / `api` / `frontend`）、multi-stage Dockerfile、`pydantic-settings`、五張表的 models 與 `UTCDateTime`、Alembic 初始 migration、JSON logging、`X-Request-ID`、錯誤 envelope、`/health/live`、`/health/ready`。
- AI 產出：`alembic revision --autogenerate` 產生的初始 migration；compose 的 `api` / `frontend` healthcheck 用 exec 形式 `["CMD", "python", "-c", "<script>"]`；`setup_logging` 把 uvicorn 三個 logger 都導到 root 的 JSON handler。
- 審查：對 MariaDB 11.7 實際跑 upgrade / downgrade / `alembic check`；在 Podman（docker CLI + `docker compose`）上實際啟動整個 stack；本機起 uvicorn 看 log 輸出。
- 發現與修正：
  1. autogenerate 的 downgrade 先 `drop_index` 再 `drop_table`。實跑 `alembic downgrade base` exit 1，停在 `DROP INDEX ix_traffic_records_service_id_observed_at`；用 mariadb client 單獨重跑得到 `ERROR 1553: Cannot drop index ... needed in a foreign key constraint`。原因：這個複合索引同時支撐 `service_id` 的 FK。改成 downgrade 只 drop table（索引隨表刪除），並加註原因。同一次手動檢查另外改了兩處：CHECK constraint 從字串改成 `sa.column(...)` expression；拿掉 PK 命名規則，因為 MariaDB 忽略 PK 名稱並在每次 migration 印 warning。修正在 commit `62b06ff`。
  2. 第一次 `docker compose up --build -d` exit 1（`container wt-m01-api-1 is unhealthy`）。uvicorn 已在跑，但 healthcheck 輸出是 `python -c` 只拿到 `import`、`SyntaxError: invalid syntax`。推測是 Podman 把 exec 形式的引數依空白切開（未查 Podman 原始碼確認）。改成 `CMD-SHELL`，之後三個容器都 healthy。修正在 commit `22199ca`。
  3. 本機 smoke run 時每個 request 出現兩行 log：uvicorn access log 與 `RequestContextMiddleware` 的 request log。改成關掉 `uvicorn.access`，只留帶 `request_id` 的那一行。修正在 commit `2e9298a`。
- 驗證（Podman 5.7.1、`docker compose` 5.6.1、MariaDB 11.7）：
  - `docker compose up --build -d` exit 0；`db` / `api` / `frontend` 皆 healthy。
  - `curl localhost:8000/health/ready` 200，body `state=HEALTHY`、計數欄位全為 0、`writer_state=NOT_STARTED`；`/docs` 200。
  - `docker compose exec db mariadb -u traffic -p... -e 'SHOW DATABASES'` exit 0，列出 `traffic`、`traffic_test`。
  - `docker compose exec -T api pytest -q` exit 0，12 passed（含 `tests/integration/test_timezone_roundtrip.py` 的 TZ-01、`tests/integration/test_migrations.py` 的 downgrade base → upgrade head → `alembic check`）。
  - `docker compose exec -T api ruff check .`、`ruff format --check .` 皆 exit 0。
  - `docker compose down -v && docker compose up --build -d` exit 0；ready 200、`traffic_test` 存在、pytest 12 passed（DEP-03 的全新 volume 路徑）。
  - 測試能抓錯的證據（本機 `uv run pytest` 連 compose 的 MariaDB）：把 `UTCDateTime` 的 `astimezone(UTC)` 拿掉，TZ-01 失敗於 `datetime(2026, 10, 31, 16, 0, 4) == datetime(2026, 10, 31, 8, 0, 4)`；拿掉 naive 檢查，失敗於 `DID NOT RAISE StatementError`。兩者還原後 12 passed。
  - asyncmy 確實執行 `init_command`：同一個 DB 上 `select(func.timediff(func.now(), func.utc_timestamp()))`，app engine 回 `0:00:00`，改用 `SET time_zone='+08:00'` 的 engine 回 `8:00:00`。
  - GitHub Actions：NOT RUN（本機無法執行）；只在本機跑過 workflow 內的 `uvx ruff@0.17.3 check / format --check backend frontend`，exit 0。
