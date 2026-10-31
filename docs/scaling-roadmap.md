# Scaling Roadmap

這是演進計畫，不是已部署能力。MVP 是單一 FastAPI process、單一 Generator、in-memory bounded buffer、單一 Writer、單一 MariaDB。每秒三筆持續一年約 9,500 萬筆（無缺樣估算）。

## 原則

擴充順序由「哪個指標先超出可接受範圍」決定，不因技術熱門而引入 Kafka 或 Kubernetes。每一階段先記錄 baseline、目標、migration / rollback、資料一致性測試，再決定是否啟用。若單實例已符合需求，只做階段 1。

## 階段

| 階段 | 啟動條件 | 變更 | 必要驗證 |
|---|---|---|---|
| 1 可營運單實例 | 要實際維運 | TLS / WSS、secret 管理、備份與 restore 演練、retention、監控告警、固定 image digest | restore 成功、key rotation、容量預警 |
| 2 Crash durability | 不能接受 process crash 遺失 ≤ 1,000 筆 | 接受邊界從 in-memory enqueue 移到 durable producer ack（Redis Streams / RabbitMQ / Kafka 依 retention、ordering、運維成本選） | 重播、duplicate、poison event、broker 中斷 |
| 3 水平擴充 | API / WS / Writer 量測達瓶頸 | Simulator 移出 API 成為單一 Producer；Writer consumer 分區；WS 用獨立 fan-out（pub/sub），不是 consumer group | 多實例一致性、不重複生成、順序、rebalance |
| 4 長期分析 | 表量或查詢延遲影響線上寫入 | Retention、分鐘 / 小時彙總表（sum / count 分開存，平均由 sum ÷ count 算）、分區或讀副本 | 聚合對帳、查詢計畫、回填與回滾 |

## 階段 1 基線

- 前端不持有 DB credential；API 用最小權限帳號；migration 用另一個帳號。
- Secrets 不進 Git / image / log；只開必要 port；登入節流在 reverse proxy。
- Named Volume 不是備份。定義 RPO / RTO，做 restore 演練（比對筆數、checksum、sample aggregates，量測恢復時間）。
- Migration 是受控部署步驟，不在多副本同時跑；不可逆 migration 前先備份。
- MariaDB 版本：11.7 是題目基線；production 查官方支援政策選仍受維護的版本，升級前測 asyncmy / Alembic / timezone / charset。

## 必要指標

| 指標 | 用途 |
|---|---|
| total_uncommitted / capacity、oldest_pending_age | 容量水位與資料新鮮度，不只看 qsize |
| batch_size、commit_latency、retry_count | DB 寫入效率；區分 transient 與永久錯誤 |
| API latency / error rate / auth failure | 服務與安全狀態 |
| active_ws、slow_client_disconnects | fan-out 與慢 client |
| generator_pause_seconds | 採樣缺口；分析時不可當零流量 |
| pool checked-out / overflow | 避免擴 API 把 DB 連線壓垮 |

門檻與 SLO 依實測 baseline 設定；本文件不虛構 p95 / uptime。

## 階段 2 的語意變更

Durable broker 後，「已接受」改為 broker ack；WS 可在 broker 接受後推送（仍是 Real-time First），歷史延遲重新定義。Consumer commit 後才 ack；重送靠 `unique(event_id)` 冪等。Poison event 要有限重試 + DLQ + 人工流程。Broker 本身也要 backpressure（磁碟滿、retention、積壓），不是把無界問題從記憶體搬到磁碟。改動時更新 ADR-001 與 ADR-003。

## Load / Chaos

逐步提高 producer rate、WS client 數、歷史查詢併發；固定硬體與 dataset；記 p50 / p95 / p99、錯誤率、記憶體、pool。成功量以 commit 與事件對帳計算，不以發送量計算。故障注入：DB 停止、網路延遲、慢 client、Writer crash、API kill、migration failure。每階段「目標達成、對帳通過、故障可恢復、操作文件可執行」才進下一階段。
