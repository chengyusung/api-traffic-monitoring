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
    RX["WS Receiver thread<br/>deque(maxlen=300)"]
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

需要 Docker Engine 24+ 與 Docker Compose v2。

```bash
git clone <repo-url> && cd api-traffic-monitoring
cp .env.example .env            # 至少修改 JWT_SECRET_KEY（openssl rand -hex 32）與密碼
docker compose up --build       # 第一次約 2–3 分鐘
```

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

`SEED_DEMO_DATA=true` 時建立。密碼來自 `.env`；以下是 `.env.example` 預設值，僅供本機 demo。

| 角色 | Email | 密碼 | 能做什麼 |
|---|---|---|---|
| ADMIN | admin@example.com | Admin1234! | 全部，含使用者管理、threshold、系統日誌與狀態 |
| USER | user@example.com | User1234! | 讀取、新增、批量匯入、改刪自己建立的資料 |
| VIEWER | viewer@example.com | Viewer1234! | 只讀：列表、即時監看、分析、Excel 匯出 |

完整權限矩陣見 [ADR-002](docs/decisions/002-authentication-rbac.md)。bootstrap admin 由 `ADMIN_EMAIL` / `ADMIN_PASSWORD` 決定，只在「尚無任何 ADMIN」時建立；沒有硬編碼密碼，也沒有未保護的升權 endpoint。

## 本地運行（不用 Docker）

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 與一個可連線的 MariaDB 11.7。最簡單的做法是只啟動 compose 的 `db`（`docker compose up -d db`），它把 3306 綁在 `127.0.0.1`。

```bash
cp .env.example .env     # 把 DATABASE_URL / TEST_DATABASE_URL 的 @db: 改成 @127.0.0.1:

# 後端
cd backend
uv sync
uv run --env-file ../.env alembic upgrade head
uv run --env-file ../.env uvicorn app.main:app --reload --port 8000
uv run --env-file ../.env pytest -q     # 測試（traffic_test 由 db/init 建立）

# 前端（另一個終端）
cd frontend
uv sync
API_BASE_URL=http://localhost:8000/api/v1 \
API_WS_URL=ws://localhost:8000/api/v1/ws/traffic \
uv run streamlit run app.py
```

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
| 批量導入 CSV / JSON | `POST /imports`，All-or-Nothing；範例 [`data/sample_traffic.csv`](data/sample_traffic.csv)、[`data/sample_traffic.json`](data/sample_traffic.json) |
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

## 開發方式（AI 輔助）

本專案在約 **__ 小時**內完成，使用 **__（例：Claude Code）** 作為 coding agent。

我負責：需求拆解與 scope 取捨、架構與三份 ADR 的決策、[`CLAUDE.md`](CLAUDE.md) 的規範與禁止事項、每個 milestone 的 code review、測試案例設計與驗證、文件最終定稿。

AI 負責：模組骨架與 CRUD 樣板、Pydantic schema、Alembic migration 初稿、測試程式初稿、文件格式整理。

審查過程中發現並修正的問題逐筆記在 [`docs/ai-review-log.md`](docs/ai-review-log.md)。設計階段已有三筆：MariaDB 時間欄位不存時區（初版用 `DateTime(timezone=True)`）、官方 image 不會建立測試資料庫、DB 故障時 admin 端點因授權依賴 DB 而不可用。實作階段的紀錄隨每個 milestone 補上；AI 第一版正確的部分也記錄驗證了什麼，不編造錯誤。

Commit history 依 [`docs/plan.md`](docs/plan.md) 的 milestone 分組，每個 commit 說明做了什麼與怎麼驗證。

## 測試

```bash
docker compose exec api pytest -q
docker compose exec api ruff check . && docker compose exec api ruff format --check .
```

測試層次、必測案例與執行證據見 [`docs/testing.md`](docs/testing.md)。所有涉及 DB 的測試都跑在 compose 內的真實 MariaDB 11.7 上，不用 SQLite 代替。 測試庫 `traffic_test` 由 `db/init/01-create-test-db.sh` 在 volume 首次初始化時建立；已有 volume 的環境要 `docker compose down -v` 重建。這支腳本只做資料庫建立與授權（基礎設施佈建）；應用程式的所有資料存取仍完全透過 SQLAlchemy ORM。

## 交付狀態

圖例：✅ PASS、❌ FAIL、⏸ 未實作、⬜ 未執行。證據欄填 commit / 命令 / 截圖位置。

| ID | 驗收 | 狀態 | 證據 |
|---|---|---|---|
| DEP-01 | 乾淨 clone → `docker compose up --build` → Swagger、UI 登入 | ⬜ | |
| DEP-02 | `down` / `up` 後資料保留 | ⬜ | |
| AUTH-01 | VIEWER 不可寫；USER 不可動他人資料；register 不可升權 | ⬜ | |
| AUTH-02 | WebSocket 無 token / 過期 → 4401 | ⬜ | |
| HEALTH-01 | 停 DB 時 `/health/ready` 不需認證回 503，body 含 DEGRADED 與計數 | ⬜ | |
| REC-01 | CRUD、分頁 / 篩選 / 排序、bulk | ⬜ | |
| IMP-01 | CSV / JSON 匯入 All-or-Nothing | ⬜ | |
| BUF-01 | 100 筆或 5 秒 flush；容量含 in-flight | ⬜ | |
| RETRY-01 | DB 停 30 秒恢復後 drain，`event_id` 無重複 | ⬜ | |
| WS-01 | Live 先於 DB 出現事件；rerun 不重複 receiver | ⬜ | |
| ANA-01 | summary / categories / trends 對帳；Excel 可開 | ⬜ | |
| ADM-01 | 使用者管理、threshold、系統日誌、系統狀態 | ⬜ | |
| CI-01 | GitHub Actions：ruff + pytest + docker build | ⬜ | |

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
- 未做：rate limit、refresh token、多實例、broker。

## 授權

MIT
