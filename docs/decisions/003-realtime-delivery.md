# ADR-003：WebSocket Real-time First Delivery

- 狀態：Accepted（2026-10-31）
- 相關：[architecture.md §3、§7](../architecture.md)、[ADR-001](001-ingestion-buffer.md)、[ADR-002](002-authentication-rbac.md)

## Context

Live Monitor 要每秒呈現三個 Service 的觀測與異常。歷史資料由 MariaDB 提供，但 Writer 是批次寫入（ADR-001）：如果即時畫面等 commit，使用者會看到五秒一跳的圖，而且 DB 一中斷即時監看就全停。

## Decision

**Enqueue-first（Real-time First）**：事件順序 `Generate → Validate → Reserve → Enqueue → Broadcast`。Broadcast 在 enqueue 成功後立即進行，不等待 commit、不與 commit 共用 transaction。Enqueue 代表「系統已接受」，不是「已持久化」。

| 狀態 | 保證 |
|---|---|
| 已接受 | 進入容量受控的記憶體管線（ADR-001） |
| 已推送 | 嘗試送給當時有效的連線；不代表 commit，也不代表 client 已收到 |
| 已持久化 | DB commit 成功或冪等確認 |
| Delivery | best-effort；無 replay、無 client ack |
| Ordering | 單一連線內依 broadcast 順序；不保證跨連線或重連後的全域順序 |
| Reconnect | 只收後續事件；斷線區間由使用者透過 REST 歷史查詢補看 |

**Endpoint 與 wire schema**：`WS /api/v1/ws/traffic`。每則訊息一個 JSON 物件：

```json
{
  "type": "traffic.observed",
  "event_id": "11111111-1111-4111-8111-111111111111",
  "service_slug": "payment",
  "title": "Payment API Traffic",
  "value": 81,
  "source": "SIMULATOR",
  "observed_at": "2026-10-31T11:30:06Z",
  "threshold_applied": 80,
  "is_anomaly": true
}
```

沒有 `id` 與 `persisted_at`，因為推送時尚未 commit。另有 `{"type": "status", "state": "HEALTHY|DEGRADED|UNAVAILABLE", "total_uncommitted": 12}`，在連線建立時送一次、之後狀態變化時推送。不做 heartbeat：正常每秒有三則訊息，client 以「超過 10 秒無訊息」判定 stale。

**認證**：handshake 時以 `Authorization: Bearer <jwt>` header 驗證。Streamlit server 是 WS client，可以帶 header；token 不放 URL，避免進 proxy / access log。驗證失敗以 close code `4401` 拒絕。Handshake 時 DB 不可用、無法重讀 user，則以 close `1013`（Try Again Later）拒絕，與超過連線上限相同；連線上限在重讀 user 之前檢查。Connection Manager 記錄每個連線的 `exp`，每次 send 前檢查，到期即 close `4401`，client 提示重新登入。

**慢速 / 失敗 client 隔離**：每個連線一個 `asyncio.Queue(maxsize=100)` 與一個 sender task；broadcast 只對每個連線做 `put_nowait`，滿了就 close `1008` 並清理。單一連線的 send 失敗只移除該連線，不影響其他連線、Writer 或 reservation。零 client 時 Generator / Writer 照常運作。連線上限 100，超過拒絕。

**Streamlit receiver**：每個 session 最多一個 receiver thread（`websockets` sync client），以 `st.session_state["ws_receiver"]` 持有，rerun 不重建。Receiver 是 daemon thread，只把訊息 append 到 `collections.deque(maxlen=400)`（近 120 秒 × 每秒 3 筆 = 360 筆，再留一點餘裕），不碰 `st.session_state` 或任何 `st.*` API——Streamlit 只保證 script thread 可以安全使用這些。UI 用 `st.fragment(run_every=1)` 在 script thread 讀 deque 更新折線圖（近 120 秒）與柱狀圖，並標示「即時（尚未持久化）」。以 `event_id` 去重。重連退避上限 1 → 2 → 4 … 最大 30 秒，加 full jitter；連線維持 10 秒以上才從 1 秒重來。Receiver 迴圈遇到任何例外都記 log 後重連，thread 不因此結束；若 thread 仍意外結束，下一次 rerun 換一個新的 receiver。畫面顯示 connected / reconnecting / stale 與最後事件時間。登出或 token 到期時以 `threading.Event` 停止 thread、清 token、清 deque。Streamlit 沒有可靠的 session 結束 hook：使用者直接關分頁時 thread 會殘留到 token 到期或 server 關閉連線，server 端 `WS_MAX_CONNECTIONS=100` 兜底。

**Manual / Import 不廣播**：它們走同步 commit 路徑，不進 Queue；Live Monitor 只顯示 Simulator 流量。

## Alternatives considered

| 方案 | 不選的原因 |
|---|---|
| 前端輪詢 DB | 受批次延遲影響；不是即時推送；題目明文要 WebSocket |
| Commit-first WebSocket | DB 故障即時監看全停；每秒三筆 commit 違背批次設計 |
| SSE | 可行，但題目指定 WebSocket，不另維護第二套通道 |
| 首則訊息帶 token | Streamlit 可帶 header，不需要；若未來改 browser client 再評估 ticket / cookie |

## Consequences

- 得到：即時畫面與持久化解耦；DB 故障期間已連線的 Live 可以持續到 buffer 滿為止；慢 client 不拖累全體。
- 付出：Live 與 Analytics 暫時不一致（正常約 5 秒）；process crash 時已顯示的事件可能消失；斷線區間無 replay；DB 故障期間無法建立新連線（`1013`），已建立的連線不受影響。
- 測試用 barrier 暫停 Writer，證明 client 先收到、DB 尚無該事件；見 [testing.md](../testing.md) WS-*。

## Open items（已知未做，不是待定）

- 跨實例 fan-out、durable replay、client ack → Roadmap 階段 3。
- Browser 直連 WS 的認證方式（目前只有 Streamlit server 作 client）。
