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

### R-004 <任務名稱>

- 任務：
- AI 產出：
- 審查：
- 發現與修正：
- 驗證：
