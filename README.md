# API Traffic Monitoring

即時資料分析與監控系統。模擬 Auth / Orders / Payment 三個 API Service 每秒的 Request Count，經有界記憶體 Queue 批次寫入 MariaDB，並透過 WebSocket 即時推送到 Streamlit；提供 JWT 認證與三種角色權限、資料 CRUD 與 CSV / JSON 批量匯入、歷史統計與 Excel 匯出、Admin 系統管理。

| | |
|---|---|
| 後端 | FastAPI、SQLAlchemy 2 async（asyncmy）、Alembic、Pydantic v2 |
| 前端 | Streamlit 多頁面 |
| 資料庫 | MariaDB 11.7 |
| 部署 | Docker multi-stage、Docker Compose |

## 目錄

- [系統架構](#系統架構)
- [快速開始（Docker）](#快速開始docker)
- [測試帳號](#測試帳號)
- [本地運行（不用 Docker）](#本地運行不用-docker)
- [API 文件](#api-文件)
- [功能對照](#功能對照)
- [專案結構](#專案結構)
- [設計文件](#設計文件)
- [開發方式（AI 輔助）](#開發方式ai-輔助)
- [測試](#測試)
- [交付狀態](#交付狀態)
- [已知限制](#已知限制)

## 系統架構

```mermaid
flowchart LR
  subgraph browser["瀏覽器"]
    U["使用者"]
  end

  subgraph frontend["frontend · Streamlit"]
    UI["多頁面 UI<br/>Login · Live · Records · Analytics · Admin"]
    RX["WS Receiver thread<br/>deque(maxlen=400)"]
  end

  subgraph api["api · FastAPI（單 worker）"]
    REST["REST /api/v1<br/>JWT · RBAC · Ownership"]
    WS["WS /api/v1/ws/traffic<br/>Connection Manager"]
    GEN["Generator<br/>3 events / s · threshold snapshot"]
    Q[("Bounded Queue<br/>reservation ≤ 1,000")]
    WR["Batch Writer<br/>100 筆 or 5 s · retry · 冪等"]
  end

  subgraph db["db · MariaDB 11.7"]
    DB[("traffic_records · users<br/>api_services · import_batches<br/>system_logs")]
    VOL[/"Named Volume"/]
  end

  U --> UI
  UI -- "HTTP" --> REST
  RX -- "WebSocket" --> WS
  GEN -- "validate · reserve · enqueue" --> Q
  GEN -- "broadcast" --> WS
  Q --> WR
  WR -- "SQLAlchemy async · asyncmy" --> DB
  REST -- "SQLAlchemy async · asyncmy" --> DB
  DB --- VOL
```

兩條資料路徑：

- **Simulator（Real-time First）**：`Generate → Validate → Reserve → Enqueue → Broadcast`。WebSocket 不等 DB commit，Writer 以「滿 100 筆或 5 秒」批次寫入，所以即時畫面會比歷史查詢早約 5 秒。容量上限 1,000 筆未提交事件，滿了 Generator 暫停，不丟資料、不無限成長。
- **Manual / Import**：`REST → JWT / RBAC → Validation → Transaction → Commit → Response`。成功回覆代表已落盤。

為什麼這樣設計、不選什麼、代價是什麼，見 [`docs/decisions/`](docs/decisions/)。

## 快速開始（Docker）

需要 Docker Compose v2。所有驗證都在 macOS 上以 Podman 5.7 經由 docker CLI 執行；Docker Engine 沒有測過，GitHub Actions workflow 也還沒有跑過。

```bash
git clone <repo-url> && cd api-traffic-monitoring
cp .env.example .env
python3 -c "import re,secrets;s=open('.env').read();open('.env','w').write(re.sub(r'(?m)^JWT_SECRET_KEY=.*',lambda m:'JWT_SECRET_KEY='+secrets.token_hex(32),s))"
docker compose up --build
```

第二行指令把 `.env` 裡的 `JWT_SECRET_KEY` 換成新產生的 64 位十六進位字串，macOS 與 Linux 都能用。這一步不能省，因為 `JWT_SECRET_KEY` 含 `change-me` 時 api 會拒絕啟動（`backend/app/core/config.py`），避免有人帶著公開的範例密鑰上線。資料庫密碼的預設值僅供本機 demo，要對外時請一併修改。

| 服務 | URL |
|---|---|
| Streamlit | http://localhost:8501 |
| Swagger UI | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| Readiness | http://localhost:8000/health/ready |

`api` 容器啟動時依序執行：`alembic upgrade head` → 建立 bootstrap admin（若尚無 ADMIN）→ 載入 demo 帳號與資料（`SEED_DEMO_DATA=true`）→ 啟動 uvicorn（單 worker）。`frontend` 等 `api` healthy 才啟動。

```bash
docker compose logs -f api                 # API log（JSON）
docker compose exec api pytest -q          # 跑測試（用 compose 內的 MariaDB）
docker compose exec api alembic revision --autogenerate -m "describe change"
docker compose stop db && sleep 30 && docker compose start db   # 故障演練：看 Live 持續、恢復後 drain
docker compose down                        # 停止，保留資料
docker compose down -v                     # 停止並刪除資料 volume
```

## 測試帳號

`SEED_DEMO_DATA=true` 時建立，並以 USER 匯入 `data/sample_traffic.json` 當範例資料。密碼來自 `.env`；以下是 `.env.example` 預設值，僅供本機 demo。

| 角色 | Email | 密碼 | 能做什麼 |
|---|---|---|---|
| ADMIN | admin@example.com | Admin1234! | 全部，含使用者管理、threshold、系統日誌與狀態 |
| USER | user@example.com | User1234! | 讀取、新增、批量匯入、改刪自己建立的資料 |
| VIEWER | viewer@example.com | Viewer1234! | 只讀：列表、即時監看、分析、Excel 匯出 |

完整權限矩陣見 [ADR-002](docs/decisions/002-authentication-rbac.md)。bootstrap admin 由 `ADMIN_EMAIL` / `ADMIN_PASSWORD` 決定，只在「尚無任何 ADMIN」時建立；沒有硬編碼密碼，也沒有未保護的升權 endpoint。

## 本地運行（不用 Docker）

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 與一個可連線的 MariaDB 11.7。最簡單的做法是只啟動 compose 的 `db`（`docker compose up -d db`），它把 3306 綁在 `127.0.0.1`。

`.env` 的 `DATABASE_URL` 與 `TEST_DATABASE_URL` 指向主機名 `db`，只有 compose 網路內解析得到。主機上直接跑會連線失敗（`Can't connect to MySQL server on 'db'`）。所以另存一份 `.env.local`，把主機名換成 `127.0.0.1`，`.env` 留給 compose 使用。

```bash
cp .env.example .env     # 先改 JWT_SECRET_KEY
sed 's/@db:/@127.0.0.1:/' .env > .env.local    # .env.local 已列入 .gitignore
docker compose up -d --wait db

# 後端
cd backend
uv sync
uv run --env-file ../.env.local alembic upgrade head
uv run --env-file ../.env.local uvicorn app.main:app --reload --port 8000
uv run --env-file ../.env.local pytest -q     # 測試（traffic_test 由 db/init 建立）

# 前端（另一個終端）
cd frontend
uv sync
API_BASE_URL=http://localhost:8000/api/v1 \
API_WS_URL=ws://localhost:8000/api/v1/ws/traffic \
uv run streamlit run app.py
```

不想建檔時，也可以在命令前直接給變數，例如 `DATABASE_URL=mysql+asyncmy://traffic:change-me@127.0.0.1:3306/traffic JWT_SECRET_KEY=$(openssl rand -hex 32) uv run alembic upgrade head`。`app/core/config.py` 在 import 時就讀取設定，所以 `DATABASE_URL` 與 `JWT_SECRET_KEY` 兩個必填值缺一個都會失敗。

## API 文件

- Swagger UI：http://localhost:8000/docs（啟動後）
- 契約摘要、列表參數、錯誤格式：[`docs/architecture.md` §6](docs/architecture.md#6-api-contract)
- WebSocket wire schema：[ADR-003](docs/decisions/003-realtime-delivery.md)

所有業務路徑前綴 `/api/v1`；認證用 `Authorization: Bearer <token>`，token 由 `POST /api/v1/auth/token` 取得（OAuth2 password form，Swagger 右上角 Authorize 可直接登入）。

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/token \
  -d 'username=admin@example.com&password=Admin1234!' | jq -r .access_token)
curl -s localhost:8000/api/v1/records?page_size=5 -H "Authorization: Bearer $TOKEN" | jq
curl -s "localhost:8000/api/v1/analytics/summary?service=payment" -H "Authorization: Bearer $TOKEN" | jq
```

## 功能對照

| 題目需求 | 實作 |
|---|---|
| 使用者註冊 / 登入（JWT） | `POST /auth/register`、`POST /auth/token`、`GET /auth/me`；Streamlit Login 頁 |
| 角色與權限（Admin / User / Viewer） | `require_roles` dependency + service 層 Ownership；矩陣見 ADR-002 |
| 資料 CRUD，分頁 / 篩選 / 排序 | `/records`（`page`、`page_size`、`sort`、`service`、`source`、`is_anomaly`、`start`、`end`、`q`）；Records 頁 |
| 更新 / 刪除僅限創建者或 Admin | 以 DB `created_by` 判斷；Simulator 資料（`created_by = NULL`）只有 Admin 可動 |
| 批量導入 CSV / JSON | `POST /imports`，All-or-Nothing；範例 [`data/sample_traffic.csv`](data/sample_traffic.csv)、[`data/sample_traffic.json`](data/sample_traffic.json)。seed 匯入 JSON，CSV 留給手動匯入 |
| 每秒隨機資料生成器 | `backend/app/realtime/generator.py`：三個 Service、固定 seed、可重現 spike |
| WebSocket 即時推送 | `WS /api/v1/ws/traffic`，Real-time First（ADR-003） |
| 即時折線圖 / 柱狀圖 | Live Monitor 頁，背景 receiver + `st.fragment(run_every=1)` |
| 數值超過閾值標記 | `value > alert_threshold`，threshold snapshot 存在每筆事件上，Admin 可改 |
| 統計 / 時間範圍 / 分類聚合 / 趨勢 / Excel | `/analytics/summary`、`/categories`、`/trends`、`/export`；Analytics 頁 |
| 定期或條件觸發批次寫入 | 有界 Queue + Batch Writer，100 筆或 5 秒，退避重試與冪等（ADR-001） |
| Admin：使用者、權限、系統日誌、DB 狀態、歷史查詢 | `/admin/users`、`/admin/logs`、`/admin/system`（buffer / pool / health）；Admin 頁 |
| SQLAlchemy ORM、禁止原生 SQL | 全部查詢用 ORM / expression，含聚合與 batch insert；`CLAUDE.md` 列為硬規則 |
| Alembic、連線池、asyncmy | `backend/alembic/`；`pool_size=5, max_overflow=10, pool_pre_ping, pool_recycle=1800` |
| Swagger、錯誤處理、日誌 | `/docs`；統一 error envelope 含 `request_id`；stdout JSON log + `system_logs` 稽核 |
| 健康檢查、故障觀察 | `/health/live`、`/health/ready`（不需認證，body 含 buffer / writer 計數，DB 故障時仍可觀察）；WebSocket `status` 訊息 |
| Docker multi-stage、Compose、Volume、環境變數 | `backend/Dockerfile`、`frontend/Dockerfile`、`docker-compose.yml`、`.env.example` |

## 專案結構

```
.
├── README.md
├── CLAUDE.md                    # 給 coding agent 與開發者的專案規範
├── .env.example
├── docker-compose.yml
├── .github/workflows/ci.yml     # ruff + pytest + docker build
├── db/init/01-create-test-db.sh # volume 首次初始化時建立 traffic_test
├── backend/
│   ├── Dockerfile               # multi-stage，非 root 執行
│   ├── alembic/                 # migrations
│   ├── app/
│   │   ├── api/                 # routers：auth, records, imports, services, analytics, admin, health, ws
│   │   ├── core/                # config, security, logging, errors
│   │   ├── models/              # SQLAlchemy models
│   │   ├── schemas/             # Pydantic（Create / Update / Read 分開）
│   │   ├── repositories/
│   │   ├── services/            # ownership、threshold snapshot、交易協調、稽核
│   │   ├── ingestion/           # reservation, queue, batch writer, retry
│   │   └── realtime/            # generator, connection manager
│   └── tests/                   # unit / api / integration
├── frontend/
│   ├── Dockerfile
│   ├── app.py                   # Login
│   ├── pages/                   # Live Monitor, Records, Analytics, Admin
│   └── lib/                     # api client, ws receiver, session helpers
├── data/
│   ├── sample_traffic.csv
│   └── sample_traffic.json
└── docs/
    ├── architecture.md
    ├── plan.md
    ├── testing.md
    ├── scaling-roadmap.md
    ├── ai-review-log.md
    ├── development-process.md
    ├── project-slides.html
    └── decisions/
        ├── 001-ingestion-buffer.md
        ├── 002-authentication-rbac.md
        └── 003-realtime-delivery.md
```

## 設計文件

| 文件 | 內容 |
|---|---|
| [系統架構](docs/architecture.md) | 技術基線、資料模型、API 契約、健康狀態、日誌與稽核 |
| [ADR-001 Ingestion Buffer](docs/decisions/001-ingestion-buffer.md) | 有界 Queue、批次寫入、重試、冪等、crash 邊界 |
| [ADR-002 Auth / RBAC](docs/decisions/002-authentication-rbac.md) | JWT 政策、角色矩陣、Ownership、bootstrap |
| [ADR-003 Real-time Delivery](docs/decisions/003-realtime-delivery.md) | Real-time First、wire schema、慢 client 隔離、Streamlit receiver |
| [Scaling Roadmap](docs/scaling-roadmap.md) | 什麼情況下才引入 broker、多實例、彙總表 |
| [測試計畫](docs/testing.md) | 測試層次、必測案例、驗收證據表 |
| [開發計畫](docs/plan.md) | Milestones、退出條件、砍掉的範圍 |
| [AI Review Log](docs/ai-review-log.md) | AI 產出 → 審查 → 修正 → 驗證 的逐筆紀錄 |
| [docs/development-process.md](docs/development-process.md)（開發經過） | 各 milestone 怎麼做、怎麼驗證 |
| [docs/project-slides.html](docs/project-slides.html)（專案簡報） | 專案簡報 |

## 開發方式（AI 輔助）

實作與驗收約 **5 小時**（從第一個實作步驟 2026-10-31 15:37 UTC 到驗收完成 20:42 UTC 的實際時間，5 小時 5 分），使用 **Claude Code** 作為 coding agent。需求拆解、三份 ADR、`CLAUDE.md` 與測試計畫在實作之前完成，這段時間沒有記錄，所以不計入。

我負責：需求拆解與 scope 取捨、三份 ADR 的決策、[`CLAUDE.md`](CLAUDE.md) 的規範與禁止事項、測試計畫，以及最後的審查與答辯。這些文件在寫任何程式之前，由我在 AI 協助下寫成。

AI 負責：每個 milestone 的實作。coding agent 依一份寫好的任務說明（範圍、退出條件、硬規則）逐個 milestone 實作，包含程式、測試與 migration。每個 milestone 合併前，另一個獨立的 reviewer 重新驗證：在真實 MariaDB 上 `docker compose up`、在 api 容器內跑 pytest、用 curl 對照驗收步驟。認證（M02）與即時推送（M03）兩個 milestone 另外做了一次多模型對抗審查，採納與駁回的項目記在 [`docs/ai-review-log.md`](docs/ai-review-log.md) 的 R-005、R-006。

審查過程中發現並修正的問題逐筆記在 [`docs/ai-review-log.md`](docs/ai-review-log.md)，目前有 R-001 到 R-008。AI 第一版正確的部分也記錄驗證了什麼，不編造錯誤。

Commit history 依 [`docs/plan.md`](docs/plan.md) 的 milestone 分組，commit 是 Conventional Commits 的單行標題；驗證過程與結果在 review log 與 [`docs/testing.md`](docs/testing.md)。

## 測試

```bash
docker compose exec api pytest -q
docker compose exec api ruff check . && docker compose exec api ruff format --check .
```

測試層次、必測案例與執行證據見 [`docs/testing.md`](docs/testing.md)。所有涉及 DB 的測試都跑在 compose 內的真實 MariaDB 11.7 上，不用 SQLite 代替。 測試庫 `traffic_test` 由 `db/init/01-create-test-db.sh` 在 volume 首次初始化時建立；已有 volume 的環境要 `docker compose down -v` 重建。這支腳本只做資料庫建立與授權（基礎設施佈建）；應用程式的所有資料存取仍完全透過 SQLAlchemy ORM。

## 交付狀態

圖例：✅ PASS、❌ FAIL、⏸ 未實作、⬜ 未執行。證據欄是 commit 加上證明它的觀察結果；完整命令、exit code 與環境見 [`docs/testing.md`](docs/testing.md) 的驗收證據。環境是 macOS 上的 Podman 5.7（docker CLI），commit 2798239（M05 合併點）；Docker Engine 沒有在本機測過。

| ID | 驗收 | 狀態 | 證據 |
|---|---|---|---|
| DEP-01 | 乾淨 clone → `docker compose up --build` → Swagger、UI 登入 | ✅ | 2798239；`docker compose up --build -d --wait` exit 0，ready 200、`/docs` 200、三個帳號登入 200 |
| DEP-02 | `down` / `up` 後資料保留 | ✅ | 2798239；`down` 後 `up`，record 137 仍回 200，volume 保留 |
| AUTH-01 | VIEWER 不可寫；USER 不可動他人資料；register 不可升權 | ✅ | 2798239；viewer `POST /records` 403，筆數前後都是 99；user 改刪他人資料 404；`role=ADMIN` 註冊 422 |
| AUTH-02 | WebSocket 無 token / 過期 → 4401 | ✅ | 2798239；無 token 連 WS 收到 close 4401；有效 token 第一則是 `status` |
| HEALTH-01 | 停 DB 時 `/health/ready` 不需認證回 503，body 含 DEGRADED 與計數 | ✅ | 2798239；停 DB 後不帶 token 的 `/health/ready` 回 503，`DEGRADED`，`total_uncommitted` 45 到 141；`/admin/system` 503 |
| REC-01 | CRUD、分頁 / 篩選 / 排序、bulk | ✅ | 2798239；總數 99 = 33 + 33 + 33，分頁不重疊，升降冪排序正確，`page_size=101` 422 |
| IMP-01 | CSV / JSON 匯入 All-or-Nothing | ✅ | 2798239；CSV 15 列 201；`value=-1` 整批 422 並指出第 4 列；筆數不變，batch 記 FAILED |
| BUF-01 | 100 筆或 5 秒 flush；容量含 in-flight | ✅ | 2798239；每秒取樣 307 次，`queued + in_flight + retry_pending` 恆等於 `total_uncommitted`，最大 141，未超過 1,000；`last_commit_at` 在 6 秒內推進，共 42 個不同值 |
| RETRY-01 | DB 停 30 秒恢復後 drain，`event_id` 無重複 | ✅ | 2798239；停 DB 30 秒，35 秒後 HEALTHY；多寫 153 筆 = 51 秒 × 3 筆；`count(*)` 888 = `count(distinct event_id)` 888 |
| WS-01 | Live 先於 DB 出現事件；rerun 不重複 receiver | ✅ | 2798239；三次都是 client 先收到事件、+0.01 秒查不到、+6.5 秒查得到；20 次 rerun 後 `active_connections` 為 1（AppTest，非真瀏覽器） |
| ANA-01 | summary / categories / trends 對帳；Excel 可開 | ✅ | 2798239；固定 `end` 後 payment 的 summary 313 = records total 313；categories 加總 941 = summary；趨勢 9 個 bucket 加總 313；Excel 以 openpyxl 開啟共 314 列，扣標頭 = 313 |
| ADM-01 | 使用者管理、threshold、系統日誌、系統狀態 | ✅ | 2798239；viewer 改 USER 後舊 token `POST /records` 201，改回 403；停用或降級自己 422；`/admin/logs` 有 2 筆 `user.updated` |
| CI-01 | GitHub Actions：ruff + pytest + docker build | ⬜ | GitHub Actions 在 push 之後才會跑；本機只跑過同一組 `uvx ruff` 指令（exit 0） |

## 已知限制

- **Process crash 會遺失未 commit 的事件**（最多 1,000 筆；正常約 5 秒的量）。已 commit 的資料不受影響。這是用簡單性換 crash durability 的明確取捨，升級路徑見 Roadmap 階段 2。
- Live Monitor 比 Analytics 早約 5 秒（一個 flush interval）；DB 故障時更久。
- 單一 API worker；多 worker 會複製 Generator。
- JWT 8 小時、無 refresh / revocation；停用與改角色靠每個 request 重讀 DB 生效。
- 授權每個 request 都查 DB，所以 DB 故障期間所有需認證的端點（含 `/admin/*`）回 503；故障觀察靠不需認證的 `/health/ready` 與 WebSocket `status` 訊息。`/health/ready` 此時也回 503，但 body 仍帶完整計數；readiness 與指標合在一個端點是 MVP 的簡化，production 應拆出 `/metrics`。
- DB 暫時故障後恢復（不丟資料）與應用程式 crash 後恢復（丟未 commit 資料）是兩件事；本系統只保證前者。
- WebSocket 斷線區間無 replay；用 Records 的時間區間查詢補看。
- 同 `event_id` 不同內容的衝突未偵測；永久錯誤批次只保留在記憶體，需人工介入。
- Manual / Import 的資料不廣播到 Live Monitor。
- 登入吞吐量受 CPU 限制（每次 Argon2 驗證約 28 ms，同時最多 2 個）；沒有 rate limit。
- api 容器以 `INSTALL_DEV=true` 建置，裡面有 pytest 與 ruff，所以測試能在容器內跑；production 建置要用 `--no-dev`。
- 本地運行時 `DATABASE_URL` 要指向 `127.0.0.1`，不能沿用 compose 用的 `db`（見上方「本地運行」）。
- compose 的 healthcheck 用 `CMD-SHELL`，因為 Podman 把 exec 形式的 `python -c` 引數切開。Docker Engine 上沒有驗證過，GitHub Actions 也還沒有跑過。
- 所有 DB 連線層級錯誤與 1020（snapshot conflict）都回 503 `Database unavailable`；1020 實際上是可重送的衝突，用 409 會更精確。其他 DB 錯誤回 500。
- `IMPORT_MAX_BYTES` 在檔案被讀取時才檢查，此時 Starlette 已經緩衝了整個上傳；要在邊界擋住過大的檔案，需要 reverse proxy 的大小限制。
- 匯入的資料 commit 之後，若寫 `import_batches` 失敗，client 會收到 503；沒有 `event_id` 的列重送會重複寫入。
- Streamlit 側欄對所有角色都列出 Admin 頁；非 ADMIN 點進去只看到提示，API 本身回 403。
- `system_logs` 沒有 `created_at` 與 `event_type` 的索引，`/admin/logs` 是掃表，demo 資料量下沒問題。`/admin/system` 每次呼叫對五張表做 count，資料量到百萬列時可能逼近 10 秒的 asyncmy `read_timeout`。
- 前端單元測試（`WS-02` 用到的 AppTest）第一次在乾淨環境執行時，有一次因 10 秒逾時失敗，之後三次重跑都通過（0.6 到 0.7 秒），推測是冷啟動，未證實。CI 的第一次執行可能遇到同樣的情況。
- 瀏覽器中的實際畫面（`st.fragment(run_every=1)` 的每秒刷新、圖表外觀、檔案上傳元件）沒有驗證；自動化只到 Streamlit `AppTest`。
- 未做：rate limit、refresh token、多實例、broker。

## 授權

MIT
