# ADR-002：JWT Authentication、RBAC 與 Ownership

- 狀態：Accepted（2026-10-31）
- 相關：[architecture.md §6](../architecture.md)、[ADR-003 認證段落](003-realtime-delivery.md)

## Context

題目要求 JWT 認證、Admin / User / Viewer 三種角色、「更新 / 刪除僅限創建者或 Admin」。Streamlit 是 client，不能當安全邊界：所有 REST、bulk、import、export、Admin API 與 WebSocket 都必須由 FastAPI 驗證與授權。

## Decision

**Token**：JWT、`HS256`，secret 由 `JWT_SECRET_KEY` 注入（不進 Git）。Access token 有效 8 小時（demo 方便；production 建議 15–60 分鐘加 refresh）。Claims：`sub`（user id）、`role`、`exp`、`iat`、`jti`。不做 refresh token，不做 revocation list。

**每個 request 從 DB 重讀 user**：`get_current_user` 驗簽、檢查 `exp`、algorithm 只接受 HS256，再以 `sub` 查 `users` 確認 `is_active`。授權用 DB 的 `role`，不用 token 裡的。代價是每個 request 多一次 PK 查詢；換到的是停用與角色變更在下一個 request 立即生效，不需要 revocation 機制。DB 不可用時回 503，不 fail open。

**密碼**：Argon2id（`argon2-cffi`）；任何 response 不含 `password_hash`。登入失敗統一回 `401 UNAUTHORIZED`，不區分帳號不存在與密碼錯誤。密碼政策：長度 ≥ 8。Email 小寫正規化，靠 `unique(email)` 處理併發重複，衝突回 409。

**角色矩陣**：

| 操作 | ADMIN | USER | VIEWER |
|---|:-:|:-:|:-:|
| 讀 Records / Services / Analytics / Export | ✓ | ✓ | ✓ |
| 訂閱 WebSocket | ✓ | ✓ | ✓ |
| 建立 record / bulk / import | ✓ | ✓ | ✗ |
| 更新 / 刪除自己建立的 record | ✓ | ✓ | ✗ |
| 更新 / 刪除他人或 Simulator（`created_by = NULL`）的 record | ✓ | ✗ | ✗ |
| 修改 Service threshold | ✓ | ✗ | ✗ |
| 使用者列表、改角色、停用 | ✓ | ✗ | ✗ |
| 系統日誌、系統狀態 | ✓ | ✗ | ✗ |

Ownership 以 DB 的 `created_by` 比對，不信任 client 傳入的任何 user id。`created_by = NULL` 不屬於任何 USER。bulk 逐筆套用同一規則。無權查看與不存在一律 404。

**註冊與 bootstrap**：`POST /auth/register` 固定建立 `USER`、`is_active=true`；request 出現 `role`、`is_active`、`password_hash` 回 422。初始 Admin 由 `ADMIN_EMAIL` / `ADMIN_PASSWORD` 環境變數在啟動時建立，條件是「目前沒有任何 ADMIN」，所以重啟不會重複建立，也沒有未保護的升權 endpoint。

**Admin 管理規則**：使用者只能停用不能刪除；Admin 不能停用或降級自己；不能停用或降級最後一個有效 ADMIN；email 不可修改。違反時回 422 並說明原因。

**實作位置**：`Depends(get_current_user)` → `Depends(require_roles("ADMIN", "USER"))` 處理角色；Ownership 在 service 層 `assert_can_modify(record, user)` 統一判斷，routers 不自己寫 if。Repository 不提供可繞過 service 的更新捷徑。

**稽核**：登入成功 / 失敗（只記 email 與結果）、註冊、角色 / 停用變更（actor、target、前後值）、record 刪除，寫入 `system_logs`，含 request_id。

## Alternatives considered

| 方案 | 不選的原因 |
|---|---|
| 只在 UI 隱藏按鈕 | 直接呼叫 API 就越權 |
| Server-side session + cookie | 撤銷容易，但與 WebSocket 及 Streamlit server-to-server 呼叫整合較麻煩；MVP 不需要撤銷 |
| 信任 token 內的 role、不查 DB | 省一次查詢，但角色變更要等 token 過期才生效 |
| OAuth / SSO / MFA | 題目沒有要求；production 需求確認後再評估 |

## Consequences

- 得到：REST 與 WebSocket 共用一套身分規則；角色變更即時生效；API 無狀態。
- 付出：auth 依賴 DB 可用性（DB 掛時新 request 全部 503）；8 小時 token 洩漏後無法主動撤銷，production 必須縮短 TTL。
- DB 故障期間所有需認證的端點都 503，**包含 `/admin/system`**。故障觀察因此改由不需認證的 `/health/ready`（只有營運計數）與 WebSocket `status` 訊息提供；已建立的 WS 連線不受影響。若 production 需要故障期間的 admin 存取，選項是短效 user 快取（例如 60 秒內驗證過的 user 在 DB 不可用時沿用），這是明確的降級行為，要另立決策，MVP 不做。
- 測試必須直接打 API，以 ADMIN、USER A、USER B、VIEWER 四個身分覆蓋矩陣每一列的允許與拒絕；見 [testing.md](../testing.md) AUTH-*。

## Open items（已知未做，不是待定）

- Refresh token、revocation、key rotation。
- 登入節流 / rate limit（production 建議在 reverse proxy 層做）。
- Multi-tenant 或 Service 範圍授權：目前讀取是全角色共享，不是租戶隔離。
