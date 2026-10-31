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
