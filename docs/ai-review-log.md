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

### R-005 M02 Auth / Records

- 任務：M02。Argon2id 密碼雜湊、HS256 JWT（`sub`、`role`、`exp`、`iat`、`jti`）、`get_current_user`（每個 request 依 `sub` 重讀 DB、用 DB 的 role 授權、DB 不可用回 503）、`require_roles`；`/auth/register`、`/auth/token`、`/auth/me`；啟動步驟 `ensure_services` → `bootstrap_admin` → `seed_demo`；`GET /services`、`PATCH /services/{id}`；`/records` CRUD、bulk、分頁 / 篩選 / 排序、Ownership；登入、註冊、threshold 變更、record value 修改與刪除的稽核。
- AI 產出：`app/bootstrap.py` 以 `logger.info("ensure_services", extra={"created": ...})` 記錄每個步驟；API 測試用 `request.getfixturevalue("user_a")` 依參數取身分 fixture；`POST /records` 直接回傳寫入的 ORM 物件；list 的 `sort=created_at` 對應到 `persisted_at`。
- 審查：在 Podman 上實際 `docker compose up --build`；對 compose 內的 MariaDB 跑全部測試；用 curl 對照 POST 與 GET 的回覆；逐項比對 ADR-002 角色矩陣與 architecture.md §5.1、§6；對關鍵規則做 mutation check，確認測試真的會抓錯。
- 發現與修正：
  1. 第一次 `docker compose up --build -d` exit 1，api log 是 `KeyError: "Attempt to overwrite 'created' in LogRecord"`。原因：`created` 是 `logging.LogRecord` 的保留屬性，不能放進 `extra`。改成 `inserted`。這次中斷發生在 `ensure_services` commit 之後、`bootstrap_admin` 之前；修正後重跑，已建立的 Service 沒有重複，剩下的步驟補齊，等於順便驗證了步驟中途中斷後重跑會收斂。修正在 commit `75252ae`。
  2. 第一次跑 pytest：`RuntimeError: Runner.run() cannot be called from a running event loop`。原因：pytest-asyncio 1.4.1 不能在 async 測試裡用 `request.getfixturevalue` 取 async fixture。改成 `identities` fixture，一次提供 ADMIN、USER A、USER B、VIEWER 四個身分。修正在 commit `3b7c24b`。
  3. curl 對照：`POST /records` 送 `+08:00` 的 `observed_at`，回覆保留 `+08:00`；`GET` 同一筆回 `Z`。兩者是同一個時間點，但同一個資源有兩種表示。改成在 schema 邊界（`UTCInstant`）把 `observed_at`、`start`、`end` 轉成 UTC。修正在 commit `6cf28a0`。
  4. 文件缺口：architecture.md §6 的 sort allowlist 有 `created_at`，§5.1 的 `traffic_records` 卻沒有這個欄位。M02 先把 `created_at` 對應到 `persisted_at`，沒有自行決定。之後決定：sort key 改名為 `persisted_at`，與 §5.1 的欄位同名，`sort=created_at` 回 422。allowlist 只定義在 `app/schemas/records.py` 的 `RecordSort`，repository 依名稱取欄位，不再另有對照表。§6 已同步。修正在 commit `759687e`。
  5. SQLAlchemy 2.1 對 `Result.tuples()` 發出 `SADeprecationWarning`，改成直接用 `.all()`。
- 驗證（Podman 5.7.1、`docker compose`、MariaDB 11.7）：
  - `docker compose down -v` 後 `docker compose up --build -d` exit 0；db、api、frontend 皆 healthy。api log：`ensure_services` inserted `auth, orders, payment`；`bootstrap_admin` inserted true；`seed_demo` inserted 兩個 demo 帳號。`docker compose restart api` 後三步皆為空 / false，DB 內 ADMIN、USER、VIEWER 各 1，Service 3 筆。
  - `docker compose exec -T api pytest -q` exit 0，120 passed，0 skipped（含 M01 的 integration 測試）。新增：`tests/unit/test_record_rules.py`、`tests/api/test_auth.py`、`tests/api/test_services.py`、`tests/api/test_records.py`、`tests/integration/test_bootstrap.py`。`alembic check`（`test_migrations.py`）通過，M02 沒有新 migration。
  - `docker compose exec -T api ruff check .`、`ruff format --check .` 皆 exit 0。
  - curl：admin 取 token 後 `GET /api/v1/records?page_size=5` 回 200 與 `{"items","page","page_size","total"}`；viewer `POST /api/v1/records` 回 403 `FORBIDDEN`，body 的 `request_id` 與 `X-Request-ID` 相同；register 帶 `"role":"ADMIN"` 回 422 `VALIDATION_ERROR`。
  - Mutation check（本機 `uv run pytest` 連 compose 的 MariaDB，每次改一處、跑完還原）：讓 USER 可改任何 record → 7 個測試失敗；threshold 改成 `>=` → 2 個失敗；value 修改時不重算、或改用 Service 目前的 threshold 重算 → 各 1 個失敗；decode 接受 `none` → `alg none` 測試失敗；拿掉角色檢查 → 4 個失敗；`UserRead` 加入 `password_hash` → conftest 的 response hook 讓 1 個測試失敗、49 個測試在建立身分時就 error；register 改成忽略多餘欄位 → 3 個失敗；不稽核登入失敗、允許停用帳號、DB 錯誤不轉 503 → 各 1 個失敗。拿掉 desc 排序的 `id` 次排序 → 1 個失敗；拿掉 asc 排序的 `id` 次排序 → 沒有測試失敗，因為 MariaDB 對相同值剛好依 PK 順序回傳，測試無法強迫它亂序。
  - api log 中 grep `JWT_SECRET_KEY`、`ADMIN_PASSWORD`、兩個 demo 密碼、`MARIADB_PASSWORD` 的值，以及 `argon2`、`eyJ`（JWT 開頭），命中皆 0。
