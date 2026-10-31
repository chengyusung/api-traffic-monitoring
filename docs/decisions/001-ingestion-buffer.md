# ADR-001：Bounded Ingestion Buffer、Batch Writer 與 Retry

- 狀態：Accepted（2026-10-31）
- 範圍：單一 FastAPI process 內的 Simulator → MariaDB 寫入管線
- 相關：[architecture.md §3、§9](../architecture.md)、[ADR-003](003-realtime-delivery.md)、[Roadmap](../scaling-roadmap.md)

## Context

Simulator 每秒產生三筆事件。題目要求「定期或條件觸發批次寫入」；同時即時圖表不應被每筆 DB transaction 的延遲拖住，但 DB 中斷時也不能讓記憶體無限成長。我需要一個在單一 process 內、行為可以用測試證明的方案。

## Decision

採用 **有界記憶體 Queue + Batch Writer + 指數退避重試**，行為由下列 invariant 定義：

1. **容量 1,000 是「已接受但未確認 commit」的總數**，包含 Queue 中、Writer 手上、retry 等待中的事件，不是 `queue.qsize()`。用 `asyncio.Semaphore(1000)` 管 reservation，`asyncio.Queue` 只負責排隊。
2. 事件先驗證、取得 reservation、enqueue 成功，才排程 broadcast。拿不到容量就等，不 drop、不擴充。
3. **只有 commit 確認後才釋放 reservation**。`queue.get()` 不是 acknowledgment。
4. 重試沿用原事件（同 `event_id`、`observed_at`、`threshold_applied`、payload），不重建。
5. 暫時錯誤不靜默丟棄；永久錯誤不無限重試。

| 參數 | 值 |
|---|---:|
| Generator interval | 1 s（3 事件 / 秒） |
| 總未提交容量 | 1,000 |
| Batch 上限 | 100 |
| Flush interval | 5 s，從當前批次第一筆進入起算（`time.monotonic()`） |
| Retry | 初始 1 s、×2、full jitter、上限 30 s；暫時錯誤無限重試 |
| Shutdown flush timeout | 10 s |

Flush 以先到者為準：滿 100 筆，或非空批次等滿 5 秒。空 Queue 不開空 transaction。每秒三筆的正常情況下約 15 筆由時間觸發；100 筆上限主要在 DB 恢復後 drain 積壓時生效。

**Writer 狀態**：`PENDING → IN_FLIGHT → COMMITTED`；失敗則 `IN_FLIGHT → RETRY_WAIT → IN_FLIGHT`。IN_FLIGHT 與 RETRY_WAIT 都占容量。每次重試 rollback 並開新的 `AsyncSession`，不沿用失敗的 transaction。單一批次單一 transaction，用 `session.add_all()`（ORM）寫入。若 reservation 取得後、enqueue 前被 cancel，退回 reservation；enqueue 後由 Writer 接管。

**冪等**：DB 已 commit 但 ack 遺失時，重試會撞 `unique(event_id)`。處理：捕捉 `IntegrityError` → rollback → `select(TrafficRecord.event_id).where(TrafficRecord.event_id.in_(batch_ids))` 找出已存在者 → 只插入缺少的事件 → commit → 釋放整批。同 `event_id` 視為已持久化。這是 at-least-once 嘗試 + unique key 去重；不宣稱 exactly-once。

**錯誤分類**：`OperationalError`、`InterfaceError`、連線 timeout → 暫時錯誤，退避重試，WARNING log 節流為每個退避週期一筆。`DataError`、`ProgrammingError`、非 duplicate 的 `IntegrityError`（如 FK）→ 永久錯誤：批次保留在記憶體、記 ERROR 與 `system_logs`、狀態轉 UNAVAILABLE、Writer 停止取新批次。這會讓 readiness 失敗並連帶停止 Generator，需要人工介入；重啟會遺失該批次，README 明寫。

**Backpressure**：容量滿時 Generator 在 reservation 上等待，不產生新事件。完全無法寫入時從空 buffer 算起約 333 秒的容量。暫停期間是採樣缺口，Analytics 不補零。

**停止**：lifespan shutdown 先停 Generator，Writer 在 10 秒內 flush 剩餘批次，逾時記錄剩餘筆數並放棄。

## Alternatives considered

| 方案 | 不選的原因 |
|---|---|
| 每筆同步 commit 後再推送 | DB 延遲與故障直接阻塞即時體驗；每秒三個 transaction 也違背題目的批次要求 |
| 無界 Queue | DB 中斷時記憶體無上限；故障行為無法證明 |
| Redis Streams / RabbitMQ / Kafka | 解決 crash durability，但對單實例 take-home 是不必要的營運負擔；引入條件見 Roadmap 階段 2 |

## Consequences

- 得到：低延遲即時推送、DB 故障時可預期的有界行為、可以用注入時鐘與 barrier 測試的語意。
- 付出：**process crash 會遺失未 commit 的事件**（最多 1,000 筆；正常約 5 秒的量，故障期間更多）；歷史查詢落後即時畫面約一個 flush interval；單實例限制。
- 可觀測：`queued`、`in_flight`、`retry_pending`、`total_uncommitted`、`oldest_pending_age_s`、`last_commit_at`、`retry_count`、`writer_state` 同時出現在不需認證的 `/health/ready` body 與 `/admin/system`。前者是為了 DB 故障時仍看得到——授權依賴 DB（ADR-002），故障時 admin 端點本身會 503。

## Open items（已知未做，不是待定）

- 重複 `event_id` 但內容不同的衝突偵測（目前同 id 一律視為已持久化）。
- 永久錯誤批次的 durable quarantine（目前只保留在記憶體）。
- 跨程序恢復、WAL、dead-letter queue → Roadmap。
