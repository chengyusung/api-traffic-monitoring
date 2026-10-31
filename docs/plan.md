# 開發計畫

- 截止：2026-11-01 17:00
- 原則：先讓一條 end-to-end 路徑可操作，再往外加。每個 milestone 結束時系統可以啟動、可以驗證，再進下一個。
- 工具：coding agent 依 [CLAUDE.md](../CLAUDE.md) 工作；每個 milestone 一組 commit，commit 是單行 Conventional Commits subject，驗證記在 [ai-review-log.md](ai-review-log.md) 與 [testing.md](testing.md)；審查結果記到 [ai-review-log.md](ai-review-log.md)。

## 優先級

| 優先級 | 內容 | 完成標準 |
|---|---|---|
| P0 | Compose + MariaDB + Alembic；JWT + 三角色 + Records CRUD；Generator + Buffer / Writer + WebSocket；Streamlit Login + Live Monitor | 乾淨環境啟動；三個帳號登入、權限不同；即時圖會動、異常會標；停 DB 30 秒恢復後 drain 無重複 |
| P1 | Import CSV / JSON；Analytics + Excel；Admin（使用者 / 角色 / threshold / 日誌 / 系統狀態）；關鍵自動化測試 | 功能可操作、UI 簡單即可；測試全綠並填入 testing.md |
| P2 | UI 細節、CI、進階測試、操作錄影 | P0 / P1 穩定後才做 |

P0 沒完成之前不碰 P1。P1 砍得掉，P0 砍不掉。

## Milestones

| # | 優先級 | 範圍 | 退出條件 | 預算 |
|---|---|---|---|---:|
| M01 Foundation | P0 | repo 結構、compose（db healthcheck + `db/init/`、api entrypoint、frontend）、multi-stage Dockerfile ×2、`pydantic-settings`、models（含 `UTCDateTime`）、Alembic 初始 migration、JSON logging、request-id、錯誤 handler、`/health/*`（ingestion 計數欄位先回 0） | `docker compose up --build` → `/health/ready` 200、`/docs` 可開；`down -v` 後重建仍可起；`traffic_test` 存在；真的對 MariaDB 做一次 insert / select 並驗證時間 round-trip | 2.5 h |
| M02 Auth / Records | P0 | Argon2、JWT、`get_current_user`、`require_roles`、bootstrap admin、`/auth/*`、`/records` CRUD + 分頁 / 篩選 / 排序 + bulk、Ownership、`/services`、稽核 | Swagger 以 ADMIN / USER A / USER B / VIEWER 手測矩陣每一列；register 偽造 role → 422 | 3 h |
| M03 Realtime | P0 | `ingestion/`（reservation、queue、writer、retry、冪等）、`realtime/`（generator、threshold reload、connection manager）、`WS /ws/traffic`、health 計數接上、lifespan 啟停；**Streamlit Login + Live Monitor** | 瀏覽器登入後折線圖每秒更新、異常標紅、顯示「尚未持久化」；`docker compose stop db` 30 秒再 `start`：Live 持續、`/health/ready` 顯示 DEGRADED、恢復後 drain、`event_id` 無重複 | 4 h |
| M04 Data | P1 | `POST /imports`（CSV / JSON、All-or-Nothing）、`/analytics/*` 四個 endpoint、Excel；Streamlit Records 頁 + Analytics 頁 | 匯入 `data/sample_traffic.csv` 成功、故意改壞一列整批失敗；Analytics 數字與 DB 對帳；Excel 可開 | 2.5 h |
| M05 Admin + Tests | P1 | `/admin/*`、Admin 頁（薄）；buffer 單元測試（注入時鐘）、RBAC API 測試、import rollback、`UTCDateTime` round-trip、health 無認證可觀察 | `docker compose exec api pytest -q` 全綠；testing.md 狀態欄填真值 | 2 h |
| M06 Delivery | P2 | README 狀態表與「開發方式」填實、ai-review-log 補齊、`.env.example` 對齊實際設定、架構圖確認 GitHub 可渲染、GitHub Actions（ruff + pytest + docker build） | 乾淨 clone 照 README 跑一次通過 | 1 h |

總計約 15 小時。任一 milestone 超出預算 50% 就砍該 milestone 的非核心項，不向後面借時間。Live Monitor 放在 M03 是刻意的：P0 的 end-to-end 路徑要在 M03 結束時就能 demo，而不是等到所有頁面都做完。

## 結果

| # | 實際結果 | merge commit |
|---|---|---|
| M01 Foundation | compose 三個服務 healthy，`/health/ready` 200，`traffic_test` 存在，時間 round-trip 與 migration upgrade / downgrade 在真實 MariaDB 上通過 | 252d83a |
| M02 Auth / Records | Argon2、JWT、三角色、Ownership、Records CRUD 與 bulk、稽核完成；多模型審查的 16 項修正已納入 | 964ea67 |
| M03 Realtime | Buffer、Writer、Generator、WebSocket 與 Live Monitor 完成；停 DB 30 秒後 drain 且 `event_id` 無重複；審查後修正 16 項 | 6fd52c2 |
| M04 Data | CSV / JSON 匯入（All-or-Nothing）、四個 analytics endpoint、Excel、Records 與 Analytics 頁完成 | 2e351e7 |
| M05 Admin + Tests | `/admin/*` 與 Admin 頁完成；後端 339 個測試、前端 11 個測試通過 | 2798239 |
| M06 Delivery | 已合併。README 狀態表、`docs/testing.md` 驗收證據、review log、`.env.example`、專案簡報與開發經過文件完成；之後再做一次誠實性修正。乾淨 clone 的完整流程與 Docker Engine 都沒有驗證過，CI-01 在 push 後才會跑 | 4f1ebb5、9b350e2、babd5e4、9154d72、405af99、6b3e1e7（docs commits） |

## M03 實作順序

1. `ingestion/buffer.py`：`Reservation`（semaphore）+ `asyncio.Queue` + `accept(event)`；先寫測試（100 筆、5 秒、容量含 in-flight、commit 後才 release、reserve 後 cancel 退回）。
2. `ingestion/writer.py`：批次收集（size / time，`time.monotonic()`）→ transaction → release；加 retry；加 `IntegrityError` 冪等。
3. `realtime/generator.py`：三個 Service、固定 seed、threshold snapshot → `buffer.accept()` → `manager.broadcast()`。
4. `realtime/manager.py`：per-connection queue + sender task；JWT handshake；`exp` 檢查；上限 100 連線；`status` 訊息。
5. `api/ws.py` + lifespan 啟停順序：載入 threshold → 啟 Writer → 啟 Generator；停止時反向。
6. `frontend/lib/ws.py` receiver thread + Live Monitor 頁：先做最小原型（連上、deque、fragment 刷新）確認 Streamlit 版本行為，再加圖表。

## 核心展示流程（面試時照這個走）

1. `docker compose up --build` → 三個容器 healthy，`/docs` 開得起來。
2. 三個帳號登入：VIEWER 看不到新增按鈕，而且直接打 `POST /records` 回 403；USER 改 ADMIN 的資料回 404。
3. Live Monitor 折線每秒更新，Payment 超過 80 標紅，畫面寫「尚未持久化」。
4. Records 查歷史、Analytics 看 summary；指出 Live 比 Analytics 早約 5 秒，解釋為什麼。
5. `docker compose stop db` → Live 持續、`/health/ready` 變 DEGRADED、`total_uncommitted` 上升、`/admin/system` 503（解釋 auth 依賴 DB）→ `docker compose start db` → drain、`event_id` 無重複。
6. `docker compose exec api pytest -q` 全綠。
7. 翻 ADR-001 講容量 invariant，翻 ai-review-log 講一個自己抓到的問題。

## 砍掉的範圍（寫進 README「已知限制」，不做）

- barrier 式 WS 測試的自動化、完整故障演練的自動化、MariaDB 相容性矩陣。
- 同 `event_id` 不同內容的偵測、永久錯誤批次的 durable quarantine。
- refresh token、revocation、rate limit、DB 故障期間的 admin 存取。
- 多實例、broker、replay。
- Admin 頁只做必要功能，不做圖表美化。

## 風險與對策

| 風險 | 對策 |
|---|---|
| SQLAlchemy async + asyncmy 在 MariaDB 11.7 的相容問題 | M01 第一件事就是真的連上去跑 insert / select，不等到 M03 |
| MariaDB `DATETIME` 不存時區 | `UTCDateTime` TypeDecorator + `SET time_zone='+00:00'`；M01 就做 round-trip 測試 |
| 官方 image 不建測試庫 | `db/init/01-create-test-db.sh`；M01 驗證 `traffic_test` 存在 |
| Streamlit 版本沒有 `st.fragment` 或 thread 行為不如預期 | 固定 Streamlit ≥ 1.37；M03 第 6 步先做最小原型 |
| Alembic 與 async engine | `env.py` 用 `run_sync`；M01 驗證 upgrade / downgrade |
| 時間不夠 | 嚴格照預算砍；README 的「已知限制」比壞掉的功能好 |
