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
- 驗證：testing.md TZ-01 round-trip；實測結果見 testing.md 驗收證據 TZ-01（PASS）。

### R-002 測試資料庫不存在

- 任務：`.env.example` 與測試 fixture。
- AI 產出：`TEST_DATABASE_URL` 指向 `traffic_test`，compose 只設 `MARIADB_DATABASE=traffic`。
- 審查：MariaDB 官方 image 的初始化行為。
- 發現與修正：image 只建立 `MARIADB_DATABASE` 並對該庫授權，`traffic_test` 不會存在，測試第一次跑就會失敗。新增 `db/init/01-create-test-db.sh`（掛到 `/docker-entrypoint-initdb.d/`）建立 `${MARIADB_DATABASE}_test` 並授權給 `${MARIADB_USER}`。更新 architecture.md §2、§9、CLAUDE.md、`.env.example`。
- 驗證：`docker compose down -v && docker compose up -d db`，然後 `docker compose exec db mariadb -u traffic -p -e 'SHOW DATABASES'` 應列出 `traffic_test`；實測結果見 testing.md DEP-03（PASS，全新 volume）。

### R-003 DB 故障時看不到故障

- 任務：`/admin/system` 與 health 端點設計。
- AI 產出：ADR-002 決定每個 request 從 DB 重讀 user、DB 不可用時 503；ADR-001 把 buffer 指標只放在 `/admin/system`。
- 審查：兩個決策放在一起的後果。
- 發現與修正：DB 中斷時 `/admin/system` 本身 503，而那正是最需要看 queue 水位的時候。把 ingestion 計數（非敏感的營運數字）放進不需認證的 `/health/ready` body；`/admin/system` 保留 pool 與資料表統計。更新 architecture.md §9、ADR-001 Consequences、ADR-002 Consequences。
- 驗證：testing.md HEALTH-01（PASS）。

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
  - 多模型對抗審查（三個互相獨立的 reviewer 各自審查 M02，共 40 筆，無 critical）：三者都確認核心規則成立（HS256 固定、必要 claim、以 DB 的 role 授權、無權限回 404、登入失敗稽核先於 401 commit、排序白名單、`extra=forbid`、無原生 SQL）。採納 16 項並已修正，主要是 Argon2 雜湊與驗證移出 event loop（三者都提出）、DB 例外統一對應 503（三者都提出）、401 加 `WWW-Authenticate`、bootstrap 與 seed 的弱密碼啟動即失敗、設定拒絕含 `change-me` 的 JWT 密鑰、PATCH 空 body 回 422 並稽核每個變更欄位，以及補齊會在規則被拿掉時失敗的測試。暫不處理的有 PATCH / DELETE 的 row lock（單 worker demo，延後）、repository 與 schema 的分層一致性。駁回 `/auth/register` 是多餘 endpoint 的意見，因為規格的功能需求 1 要求使用者註冊，ADR-002 也有定義。
  - api log 中 grep `JWT_SECRET_KEY`、`ADMIN_PASSWORD`、兩個 demo 密碼、`MARIADB_PASSWORD` 的值，以及 `argon2`、`eyJ`（JWT 開頭），命中皆 0。

### R-006 M03 Realtime

- 任務：M03。`ingestion/buffer.py`（semaphore reservation、`asyncio.Queue` 排序、commit 後才 release）、`ingestion/writer.py`（100 筆或 5 秒、一批一個 transaction、full jitter 退避、duplicate 後只補缺的、永久錯誤停機並寫 `system_logs`）、`realtime/generator.py`（三個 Service、固定 seed、threshold 每 5 秒重載、先 accept 再 broadcast）、`realtime/manager.py`（每連線一個有界 queue 與 sender task、上限 100、`exp` 檢查）、`WS /api/v1/ws/traffic`、lifespan 啟停順序、`/health/ready` 真實計數與 §9 狀態機；Streamlit Login 與 Live Monitor。
- AI 產出：buffer 與 writer 的單元測試先寫（fake session、虛擬時鐘：只有程式 sleep 時時間才前進），再寫實作。另外兩項來自規格本身：任務說明要求 handshake 失敗時「accept 前 close 4401」；ADR-003 同時規定 receiver `deque(maxlen=300)` 與 120 秒折線。
- 審查：對 buffer / writer 規則逐條做 mutation check；在 api 容器內用 `websockets` client 實測 handshake；真的停 DB 30 秒；Streamlit `AppTest` 跑兩個頁面（frontend 容器內、連真實 api）。
- 發現與修正：
  1. Mutation check 有兩條規則沒被抓到。「`queue.get()` 當 ack」只改了 await 的取出路徑，但測試裡 writer 走 `take_nowait()`，所以 8 個測試全過；改成在兩條取出路徑都釋放容量後，容量測試失敗。「reserve 與 enqueue 之間多一個 await」也全過：原本的取消測試只走到 stdlib semaphore 已處理的「已分配但被取消」路徑。新增 `test_cancelling_accept_at_any_await_point_never_leaks_capacity`，在 `accept()` 的每個 await 點取消，之後抓到這個 mutation。修正在 commit `293c3af`。
  2. 「accept 前 close 4401」：實測（同版 uvicorn 與 websockets）accept 前 close 時，client 收到的是 HTTP 403，close code 4401 不會送達。改成驗證完成前不註冊、不送任何資料，但先 accept 再 `close(4401)`，client 才讀得到 4401。程式內有註解說明原因。
  3. ADR-003 同時規定 `deque(maxlen=300)` 與「近 120 秒」：每秒 3 筆，120 秒是 360 筆，超過 300。deque 只當 thread 與 script 之間的傳遞緩衝；頁面在 script thread 以 `event_id` 去重，另存一個依 `observed_at` 只留 120 秒的視窗。沒有資料的秒數不補值。
  4. uvicorn 沒有安裝 WebSocket 實作就無法處理 upgrade。backend 加入 `websockets==17.4`，與 frontend lock 同版。
  5. 文件沒寫的情況：handshake 時 DB 不可用。決定以 close `1013`（Try Again Later）拒絕，與連線數超過上限相同；不吞例外，也不當成 4401。
  6. 第一版：Writer 永久錯誤時，Generator 不會立刻停止，而是在 buffer 滿（約 333 秒）時被 backpressure 擋住；這段期間推送的事件已被接受但不會寫入。M03 fix round 已修正（commit `64a3742`）：Generator 每個 tick 檢查 writer 狀態，writer 進入 UNAVAILABLE 或 STOPPED 後一個 tick 內停止，停止原因記在 `Generator.stopped_by` 並寫一筆 ERROR log。測試讓假的 writer 狀態在第一個 interval 內變成 UNAVAILABLE / STOPPED，預期只產生一個 tick（3 則）；拿掉檢查、或 STOPPED 不算停止，兩個 mutation 都會被抓到。
- 驗證（Podman、`docker compose`、MariaDB 11.7）：
  - `docker compose exec -T api pytest -q` exit 0，200 passed、0 skipped。新增 37 個：`test_buffer.py` 11、`test_generator.py` 8、`test_writer.py` 3（真實 MariaDB）、`test_ws.py` 6、`test_health.py` 由 3 個改為 12 個。
  - `ruff check`、`ruff format --check` 在 api 容器內與 CI 的 `uvx ruff@0.17.3 ... backend frontend` 皆 exit 0。
  - Mutation check（buffer / writer 單元測試）：8 個 mutation 全部被抓到（commit 後才 release、`queue.get()` 不是 ack、reserve 與 enqueue 之間沒有 await、backoff 上限 30 秒、retry 沿用原事件、滿 100 筆不等計時器、永久錯誤不重試、空 queue 不開 transaction）。其中「commit 前先 release」造成 2 個失敗後卡住，被 30 秒 timeout 終止。Integration：「duplicate 就判整批成功」→ 1 個失敗（缺 2 筆）；「任何 IntegrityError 都重試」→ 永遠重試，20 秒 timeout 終止。
  - 實際啟動後 20 秒：`/health/ready` 200 HEALTHY、`total_uncommitted` 0、`last_commit_at` 有值。api 容器內的 WS client：無 token 與壞 token 都收到 4401；有效 token 第一則是 `status`，之後 10.5 秒收到 31 則 `traffic.observed`（每 tick 3 則，tick 間最大間隔 1.01 秒）。
  - 停 DB 30 秒：`/health/ready` 每 5 秒回 503 DEGRADED，`total_uncommitted` 21 → 36 → 51 → 66 → 81 → 96。同時 WS client 未斷線，每約 5 秒收到 15–18 則事件，並在停機約 1 秒後收到 DEGRADED 的 `status`。`docker compose start db` 後 15 秒回到 200 HEALTHY，20 秒時 `total_uncommitted` 為 0。`select count(*), count(distinct event_id) from traffic_records` 為 2547 / 2547。`system_logs` 有 `writer.recovered`（6 次失敗後成功）。
  - `docker compose stop api`：log 依序為 `Waiting for application shutdown.` → `ingestion stopped {"abandoned_events": 0}` → `Application shutdown complete.`。
  - Streamlit（frontend 容器內 `AppTest`，連真實 api）：錯誤密碼顯示對應訊息；登入後 Live 頁顯示連線狀態、最後事件時間、系統狀態、各 Service 最新值、「即時（尚未持久化）」標示與 2 張圖；連續 rerun 20 次，api 的 `active_connections` 每次都是 1；壞 token 收到 4401 後，receiver thread 結束、token 清除、Login 頁顯示過期提示；登出後 token 與 receiver 都清除，thread 數為 0。`docker compose restart frontend` 後 log 無 traceback。
  - 多模型對抗審查（三個互相獨立的 reviewer 審查 M03，共 42 筆，其中有 critical）：三者都提出 Generator 在 Writer 永久失敗後沒有停下、慢 client 的清理路徑會漏、shutdown 時退避等待擋住關閉。採納 16 項並已修正，包括依 errno 分類 writer 錯誤（reviewer 實際執行 asyncmy 例外確認 4025 與 1054 不該重試）、每次寫入加 `COMMIT_TIMEOUT_SECONDS` 上限、Generator 在 UNAVAILABLE 或 STOPPED 時停止、慢 client 以 1008 關閉並有送出逾時、health 狀態不閃動、receiver 迴圈任何例外都重連。有一項只有單一 reviewer 提出但被實際重現（關閉時佇列裡殘留事件），所以也採納。駁回兩項：對永久錯誤批次做 bisect 或隔離（ADR-001 已決定永久錯誤停機等人工處理，並把隔離列為待決事項），以及分頁關閉後遺留的 receiver（ADR-003 明確接受，並以 `WS_MAX_CONNECTIONS` 限制上限）。
  - 未驗證：`st.fragment(run_every=1)` 在瀏覽器中每秒自動刷新（`AppTest` 只會同步執行 fragment，不會觸發計時器），以及圖表的實際外觀；兩者留給瀏覽器人工檢查。GitHub Actions：NOT RUN。

### R-007 M04 Data

- 任務：M04。`POST /imports`（multipart，CSV / JSON，All-or-Nothing）與 `GET /imports/{id}`；`/analytics/summary`、`/categories`、`/trends`、`/export`（openpyxl）；`SEED_DEMO_DATA=true` 時以 demo USER 匯入 `data/sample_traffic.csv`；Streamlit Records 頁與 Analytics 頁。
- AI 產出：匯入解析是純函式 `parse()`，每列交給 `RecordCreate` 驗證，再檢查 Service 與檔內重複的 `event_id`；資料寫入重用 `records.create_records()`（新增 `source` 參數），batch 與稽核放在下一個 transaction。錯誤格式加上選用的 `details`。Analytics 與列表共用 `filter_conditions()`；trend 用 `CAST(DATE_FORMAT(observed_at, …) AS DATETIME(6))` 分桶；export 以 server-side cursor 分批讀取，在 thread 裡寫 write-only workbook。
- 審查：對 compose 的 MariaDB 跑全部測試；13 個 mutation；100k 列計時；三個 demo 帳號的 Streamlit `AppTest`；以 curl 走一遍驗收步驟；逐條比對 architecture.md §5.3、§6、§7 與 ADR-002。
- 發現與修正：
  1. `tests/unit/test_analytics.py` 與 `tests/api/test_analytics.py` 同名，pytest 預設的 import mode 收集時報 `import file mismatch`。`pyproject.toml` 改用 `--import-mode=importlib`。commit `b0b1825`。
  2. `data/` 在 backend 的 build context 之外。第一版把它掛到 api 容器的 `/app/data`，程式用 `parents[1]` 找檔案；容器內可行，但 README「本地運行（不用 Docker）」從 `backend/` 執行時找不到。改成掛在 `/data`、程式用 `parents[2]`，容器與本機都指向 repo 的 `data/`。commit `9ffd36d`。
  3. 規格缺口：`data/sample_traffic.csv` 有 3 列固定 `event_id`。seed 匯入之後，同一個檔案再匯入一定是整批 409，這符合 §5.3。所以「curl 匯入 `data/sample_traffic.csv` 得到 201」在已 seed 的環境不會成立。規則沒有改；改用 seed 的 batch（`GET /imports/1` 為 SUCCEEDED 15/15）證明同一個檔案經同一個流程匯入成功，再用清空 `event_id` 欄的副本實測 201。
  4. 文件沒寫、自行決定並補進 architecture.md §5.3 / §6 / §7 的部分：行號（CSV 用檔案行號、標頭為第 1 行；JSON 用陣列位置）；`failure_count` 是有錯的列數；`details` 與 `errors` 最多 100 筆；`GET /imports/{id}` 只給上傳者與 ADMIN，其他人 404；`import_batch_id` 在寫 batch 的 transaction 裡補上；trends 超過 10,080 個 bucket 回 422。
  5. Mutation「忽略列錯誤、只寫有效的列」有被抓到，但抓到的是 `import_batches` 的 CHECK（`success_count` 只能是 0 或 total，MariaDB 4025）：14 列已 commit，之後 batch insert 才失敗。資料 transaction 本身沒有第二道防線，全靠先驗證全部列。
  6. 第一版的兩個測試輸入寫錯：檔內重複的測試把整列轉大寫，連 service 也變成 `PAYMENT`；IMP-01 改錯了行的值。第一次執行就失敗，修的是測試輸入，不是程式。
- 驗證（Podman、`docker compose`、MariaDB 11.7）：
  - `docker compose up --build -d` exit 0；`docker compose exec -T api pytest -q` exit 0，291 passed（原本 242）。新增 49 個：`test_import_parser.py` 30、`unit/test_analytics.py` 2、`test_import_all_or_nothing.py` 3、`api/test_imports.py` 5、`api/test_analytics.py` 8、`test_bootstrap.py` 1。api 容器內 `ruff check .`、`ruff format --check .` 皆 exit 0；`uvx ruff@0.17.3 check / format --check backend frontend` 皆 exit 0。
  - 以 user@example.com 執行 curl：`GET /imports/1`（seed）為 SUCCEEDED 15/15；原檔 409 `CONFLICT`，`details` 列出第 14、15、16 行；清空 `event_id` 的副本 201 SUCCEEDED 15/15；第 8 行 value 改成 -55 的副本 422，`details` 為 `row 8 value`，IMPORT 筆數前後都是 30。
  - `GET /analytics/summary?service=payment` 的 count、`GET /records?service=payment&page_size=1` 的 total、`/analytics/export?service=payment` 的 xlsx 列數皆為 585；加上固定 `end` 後三者皆為 530。VIEWER：`POST /imports` 403，`GET /analytics/summary` 200。
  - Mutation 13 / 13 被抓到：部分匯入、失敗時不寫 batch、沒有 DB 衝突預檢、沒有檔內重複檢查、CSV value 接受 `5.0`、`end` 改成含端點、空集合 sum 補 0、anomaly 數成全部、hour 用 minute 格式、export 忽略 filter、export 沒有上限、任何人都能讀別人的 batch、seed 不檢查既有資料。
  - 100k 列（traffic_test，測完清空）：summary 0.01 秒、categories 0.04 秒、minute trend 0.04 秒（556 個 bucket）；export 33,333 列 1.56 秒、2.39 MB；100k 列 export 回 422。約 50k 列 export 期間，10 ms ticker 量到的 event loop 最長停頓是 57 ms。
  - Streamlit `AppTest`（frontend 容器內，連真實 api）：三個帳號開兩個新頁面都沒有 exception。VIEWER 看不到新增、編輯、匯入；USER 新增後可以編輯自己的那一列。把 session 的角色假改成 ADMIN 去刪別人的資料，畫面顯示後端的 `Record not found.`。Excel 下載按鈕出現。`docker compose logs frontend` 沒有 traceback。
  - 未驗證：瀏覽器中的實際外觀與 file uploader 操作（`AppTest` 無法操作上傳元件；上傳走的 `lib/api.import_file` 已在 frontend 容器內直接呼叫確認，422 的 `details` 有正確解析）。GitHub Actions：NOT RUN。
  - 多模型對抗審查（M04 與 M05 一起審查，三個互相獨立的 reviewer 共 43 筆，1 筆 critical）：critical 是 Excel 匯出把使用者的 title 原樣寫入：`=` 開頭會變成公式，C0 控制字元會讓之後每次涵蓋那一列的匯出都回 500（reviewer 以 openpyxl 3.1.5 重現）。三者都提出公式注入、巢狀 JSON 造成 500、匯入兩段 transaction 之間的空檔。屬於 M04 的採納項目都已在修正輪處理。匯出把每個文字欄位寫成文字儲存格，title 在 schema 邊界拒絕 C0 控制字元，匯出失敗時關閉 cursor 並刪除 openpyxl 的暫存檔（`671ca02`）。巢狀 JSON、超過 12 位的整數、沒有資料列的檔案改為檔案層級 422；檔內重複的 `event_id` 不再被第一列的其他錯誤遮住；未知欄位名稱截為 64 字元；匯入途中 Service 被停用也記成 FAILED batch（`3b45cb4`）。成功的匯入改為 batch、資料列、稽核在同一個 transaction commit，`create_records()` 不再自己 commit，改由呼叫端 commit（`90c0d01`）。`Content-Length` 超過上限時在讀取前回 413（`69aaad4`）。Analytics 頁預設最近 24 小時（`3b9c55d`）。`observed_at` 拒絕 Unix time 數字（`7791eba`）。Writer 遇到被非 SIMULATOR 列佔用的 `event_id` 時記 WARNING（`cdc410f`）。CI 的 frontend 測試加入 Records 與 Analytics 頁（`2dc1d68`）。`REPORTED_ERRORS` 上限、trend bucket 上限、匯入不碰 buffer 與 WebSocket 都有了測試（`3b9c55d`、`09e077b`）。暫不處理：重複的 `Page` 模型與時間區間驗證、10,000 列匯入的 round trip 次數（只有推論，未量測）、1020 的訊息與重送前重新讀取。記錄但不修：CSV 多行引號欄位的行號是該筆記錄的最後一行（已寫進 architecture.md §5.3）；frontend 的 5 秒逾時早於 backend 10 秒的 `read_timeout`。沒有駁回的項目。M05 的採納項目與保留「最後一個有效 ADMIN」規則的決定記在 R-008。

### R-008 M05 Admin + Tests

- 任務：M05。`GET /admin/users`、`PATCH /admin/users/{id}`（只改 role 與 is_active，ADR-002 管理規則回 422）、`GET /admin/logs`（level / event_type / 時間區間 / 分頁）、`GET /admin/logs/event-types`、`GET /admin/system`；Streamlit Admin 頁；testing.md 尚未自動化的案例。另外處理前幾輪 review 留下的項目：REST 的 503 改用 errno 分類、CI 跑 frontend 單元測試、seed 改用 JSON 範例。
- AI 產出：`AuditEvent` enum 集中所有 `system_logs.event_type`，`record_event()` 只收這個型別。`update_user()` 先鎖住所有有效 ADMIN 列與目標列，再套「不能動自己」與「不能動最後一個有效 ADMIN」兩條規則，變更與 `user.updated` 稽核同一個 transaction。`/admin/system` 的 readiness 與 `/health/ready` 用同一個 `Pipeline.readiness()`，另加 pool 文字與設定、Generator 的 threshold map / `thresholds_loaded_at` / `paused_s` / `stopped_by`、一次查詢的各表筆數。`database_unavailable()` 從 writer 移到 `app/core/errors.py`，REST handler、Writer、WS handshake 共用。Admin 頁用 `lib/api` 的新函式，非 ADMIN 只看到提示。
- 審查：對 compose 的 MariaDB 跑全部測試；18 個 mutation；以 curl 走 M05 的驗收步驟；停 DB 實測 503 與 log 的 errno；frontend 容器內以 `AppTest` 連真實 api 操作 Admin 頁與 Live 頁；逐 commit 跑 ruff 與 pytest。
- 發現與修正：
  1. 第一版 `update_user()` 在認證讀取之後直接 `SELECT … FOR UPDATE`。兩個 last-admin 測試都拿到 MariaDB 1020（`Record has changed since last read`），不是 422。原因是 MariaDB 11.7 預設 `innodb_snapshot_isolation=ON`：鎖定讀取遇到 snapshot 建立後被改過的列就中止。改成先 commit 認證那個只讀的 transaction 再上鎖。commit `45fa7a8`。
  2. 拿掉 `FOR UPDATE` 的 mutation 被測試抓到，但失敗內容是另一個 transaction 的 1020，不是預期的「兩邊都成功」。所以沒有鎖時，MariaDB 會讓競爭的 request 變成 500；有鎖時是有原因的 422。
  3. 預定加入 CI 的指令 `cd frontend && uv run python -m unittest` 在本機回 `NO TESTS RAN`。`frontend/tests` 沒有 `__init__.py`，預設 discovery 不會進去。加上 `tests/__init__.py` 後找到 11 個測試。commit `c25f714`。
  4. REST 的 503 handler 依 exception 類別判斷，CHECK 4025、unknown column 1054 都會變成 503。改用與 Writer 相同的 errno 分類，其他 DB 錯誤回 500。`test_errors.py` 改用真的 asyncmy exception。commit `ee28d75`。
  5. M04 的 seed 匯入 `sample_traffic.csv`，之後手動匯入同一個檔案一定 409。seed 改成 `sample_traffic.json`，兩個檔案的固定 `event_id` 不重疊。commit `a0bbecf`。
  6. ADR-001 的錯誤分類段落還寫「`OperationalError` → 暫時錯誤」，與 M03 之後依 errno 判斷的程式不一致。已改寫。
  7. 在 API 上，最後一個有效 ADMIN 只會是操作者自己，所以「最後一個 ADMIN」規則只在並行時生效。這條改用 integration test 直接呼叫 service 驗證，testing.md 的案例說明已補上這一點。
  8. 第一版 Admin 頁測試用位置取 selectbox，取到 Level 而不是 event type。修的是測試，改用 widget key。
  9. 寫 last-admin 測試時看到的 1020 不只影響 Admin：PATCH `/records`、`/services` 也是先讀再寫，同一列在兩者之間被別的 commit 改過就會 1020。M05 之前 asyncmy 的 `OperationalError` 一律回 503；改成依 errno 分類後，它會變成 500。1020 與 deadlock 同類，重送可以成功，所以加入暫時錯誤清單，REST 仍回 503。新增一個在真實 MariaDB 上觸發 1020 的 integration test。commit `60eac2d`。
  10. 未修：frontend 頁面測試在主機上第一次執行時有一個 `AppTest` 10 秒逾時，之後 13 次都在 1 秒內完成。推測是 macOS 第一次載入新 venv 的原生程式庫時的掃描，未證實。
- 驗證（Podman、`docker compose`、MariaDB 11.7）：
  - `docker compose down -v` 後 `docker compose up --build -d --wait` exit 0。`docker compose exec -T api pytest -q` exit 0，339 passed（原本 291）。新增 48 個：`api/test_admin_users.py` 24、`api/test_admin_system.py` 14、`integration/test_last_admin.py` 2、`integration/test_snapshot_conflict.py` 1、`api/test_errors.py` 5、`unit/test_writer_errors.py` 1、`integration/test_bootstrap.py` 1。api 容器內 `ruff check .`、`ruff format --check .` 皆 exit 0。
  - `cd frontend && uv sync --frozen && uv run python -m unittest` exit 0，11 tests（原本 7 個，但預設 discovery 找不到）。
  - curl：admin 把 viewer 改成 USER 回 200，viewer 原本的 token `POST /records` 從 403 變成 201；admin 停用自己回 422 `You cannot deactivate or demote yourself.`；改 email 回 422 `email cannot be changed`；`GET /admin/system` 200，含 `Pool size: 5 …` 與 `table_counts`；user@example.com `GET /admin/users` 回 403。剛建立的 stack 上，`GET /imports/1` 是 `seed:sample_traffic.json` SUCCEEDED 6/6，user 匯入 `data/sample_traffic.csv` 回 201 SUCCEEDED 15/15。
  - 停 DB：`/admin/system` 與 `/records` 回 503 `SERVICE_UNAVAILABLE`，api log 為 `database unavailable {"error": "OperationalError", "errno": 2003}`；`/health/ready` 不帶 token 回 503 DEGRADED。DB 啟動後兩者回到 200。
  - frontend 容器內 `AppTest` 連真實 api：Admin 頁五個區塊都出現、沒有 exception；在頁面上停用自己時，畫面顯示後端原文 `You cannot deactivate or demote yourself.`；VIEWER 只看到提示。Live 頁連續 rerun 20 次後 `/admin/system` 的 `active_connections` 是 1，停止 receiver 後是 0。`docker compose logs frontend` 沒有 traceback。
  - Mutation 18 / 18 被抓到：REST 不分類就回 503、忽略 invalidated connection、1020 不算暫時錯誤、拿掉最後 ADMIN 規則、拿掉不能動自己、拿掉 `FOR UPDATE`（3 次都抓到）、拿掉先 commit、沒有變更也寫稽核、拿掉 email 的原因訊息、日誌次排序改 id 升冪、`end` 含端點、忽略 level、readiness 以 DB 不可用計算、`stopped_by` 不輸出、event types 少一個、seed 改回 CSV、每次 rerun 建新 receiver、Admin 頁不擋非 ADMIN。
  - 逐 commit：每個 commit 以 `git archive` 取出，`uvx ruff@0.17.3 check` 與 `format --check` 皆 exit 0；pytest 各 commit 都通過（依序 295、295、295、295、296、296、336、336、339 passed）。
  - 未驗證：瀏覽器中的實際畫面（`AppTest` 不會畫出來）；Streamlit 自動產生的側欄仍會列出 Admin 頁，非 ADMIN 點進去只看到提示。GitHub Actions：NOT RUN。
  - 多模型對抗審查（與 R-007 同一輪）：屬於 M05 的採納項目都已在修正輪處理。暫時錯誤清單補上 1158–1161、1203、1290、1317、1969；Writer 自己的寫入逾時移到 `is_transient()`，REST 分類裡永遠走不到的內建 `TimeoutError` 分支刪除（`51b105c`）。migration `0002` 加入 `system_logs (created_at, id)`、`system_logs (event_type, created_at)`、`traffic_records (observed_at)`、`users (role, is_active)`（`1f20db7`）。`/admin/system` 的各表筆數在 api 內快取 10 秒，Admin 頁的系統狀態也快取 10 秒（`4844f04`）。`update_user()` 鎖定後確認操作者仍是有效 ADMIN，否則回 403；沒有變更時先 rollback 釋放鎖；開頭確認 session 沒有未寫入的變更（`c784177`）。`record_event()` 以 `AuditEvent(...)` 轉換，清單外的名稱直接 raise，`level` 統一為 `AuditLevel`（`2edd11c`）。操作者檢查讓兩個 ADMIN 並行互降時，後到的一方回 403 而不是 422。「最後一個有效 ADMIN」因此由這個檢查加上「不能動自己」保證；原本那條規則的程式保留為 ADR-002 的明文規則，但已無法從 service 觸發。這個保留是 coordinator 的決定，理由是 ADR-002 明文列出這條規則，保留只多兩行程式，而且兩個 integration test 仍斷言至少留下一個有效 ADMIN。沒有駁回的項目。同一輪唯一的 critical 是匯出的公式注入與控制字元造成 500，屬於 M04，記在 R-007。

### R-009 提交前審查（2026-11-01）

- 任務：對 HEAD `e632ba0` 做提交前審查：SPEC.md 逐條對照實作、兩個測試套件、lint、以真實瀏覽器走七個 demo 步驟、停 DB 再啟動的演練。審查者只讀 SPEC 與原始碼，不改程式。
- 審查結論：沒有 P0 / P1；三項 P2。
- 發現與修正：
  1. JSON 匯入的數字超過 4,300 位數時，`json.loads` 丟的是 `ValueError` 而不是 `JSONDecodeError`，request 變成 500 而不是文件寫的 422 + FAILED batch（CSV 路徑早有 `_INTEGER` 擋住）。`_json_rows()` 在 `JSONDecodeError` 分支之後補 `except ValueError`；parser 單元測試加一個 4,301 位數的 `.json` 案例，API 測試加一個 422 + `GET /imports/{id}` FAILED 的案例。commit `a11ca7f`。
  2. Live Monitor 與 Analytics 的時間軸標成 UTC，但 Vega-Lite 在瀏覽器時區建刻度，台北的瀏覽器會把 bar 畫在 API 與表格所示 bucket 的 8 小時之後。兩頁的 x 軸改用 `scale=alt.Scale(type="utc")`，tooltip 改用預先以 UTC 格式化的字串欄位；AppTest 加一個斷言 chart spec 的 x 軸 scale 為 `utc` 的測試。commit `23b3fd6`。瀏覽器畫面由使用者確認，尚未進行。
  3. 已開啟的 Streamlit session 看不到角色變更：前端在登入時快取 user，之後不再呼叫 `/auth/me`，所以升級的使用者要重新登入才看得到寫入控制項，被降級的使用者保留的控制項會被 API 回 403。決定只寫文件、不改程式：API 是權威來源且每個 request 重讀角色；七步 demo 不會在 session 中途換角色；每頁多呼叫一次 `/auth/me` 會動到所有頁面測試，並在 DB 故障時多一個 503 的失敗面。README 已知限制加一條，Admin 頁的使用者表單加一行說明。commit `d9c424a`。
- 驗證（主機，Python 3.12）：`backend` 的 `pytest tests/unit/test_import_parser.py` 40 passed；`frontend` 的 `python -m unittest` 18 tests OK（原 17 + 1）；兩個套件 `ruff check` 與 `ruff format --check` 皆 exit 0。Mutation 2 / 2 被抓到：拿掉 `except ValueError` 後新案例以 `ValueError: Exceeds the limit (4300 digits)` 失敗；拿掉 `type="utc"` 後新測試以 `None != {'type': 'utc'}` 失敗。未在主機執行：`tests/api/test_imports.py` 的新案例需要 compose 的 MariaDB，由合併後的容器內全套測試涵蓋；瀏覽器畫面未渲染。
