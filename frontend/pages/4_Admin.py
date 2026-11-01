import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import streamlit as st

from lib import api, session, ui

ROLES = ["ADMIN", "USER", "VIEWER"]
LEVELS = [None, "INFO", "WARNING", "ERROR"]
LOG_RANGES = {name: span for name, span in ui.RANGES.items() if name != "自訂日期（UTC）"}


def everything(value: str | None) -> str:
    return "全部" if value is None else value


@st.cache_data(ttl=10, show_spinner=False)
def system_status(token: str) -> dict[str, Any]:
    # Every widget change reruns the page; the status block need not hit the API each time.
    return api.admin_system(token)


st.set_page_config(page_title="Admin", layout="wide")
session.require_login()
user = session.current_user() or {}
st.title("Admin")
if user.get("role") != "ADMIN":
    # Convenience only: every /admin route answers 403 to other roles anyway.
    st.info("這一頁只有 ADMIN 可以使用。")
    st.stop()
ui.show_flash()

st.header("系統狀態")
if system := ui.call(system_status):
    readiness, pool, generator = system["readiness"], system["pool"], system["generator"]
    tiles = st.columns(4)
    tiles[0].metric("狀態", readiness["state"])
    tiles[1].metric("未提交事件", readiness["total_uncommitted"])
    tiles[2].metric("Writer", readiness["writer_state"])
    tiles[3].metric("WebSocket 連線", readiness["active_connections"])
    st.dataframe(pd.DataFrame([readiness]), hide_index=True)
    st.caption(
        "in_flight 是 Writer 手上的批次，含收集中的那批；writer_state 為 IDLE 時也可能大於 0。"
    )
    st.code(pool["status"], language=None)
    st.caption(f"pool_size {pool['pool_size']}，max_overflow {pool['max_overflow']}")
    st.markdown(
        f"- Threshold 最後載入（UTC）：{generator['thresholds_loaded_at'] or '尚未載入'}\n"
        f"- Generator 因 buffer 滿暫停累計：{generator['paused_s']:.1f} 秒\n"
        f"- Generator 停止原因：{generator['stopped_by'] or '執行中'}"
    )
    left, right = st.columns(2)
    left.dataframe(
        pd.DataFrame(
            sorted((generator["thresholds"] or {}).items()), columns=["service", "threshold"]
        ),
        hide_index=True,
    )
    right.dataframe(
        pd.DataFrame(sorted(system["table_counts"].items()), columns=["table", "rows"]),
        hide_index=True,
    )
    st.caption("系統狀態最多每 10 秒更新一次；各表筆數是 10 秒內的近似值。")

st.header("使用者")
users_page = int(st.number_input("頁碼", min_value=1, max_value=10_000, value=1, key="users-page"))
listing = ui.call(api.admin_users, {"page": users_page, "page_size": 100})
if listing and listing["items"]:
    st.caption(f"共 {listing['total']} 位；使用者只能停用，不能刪除，email 不能修改。")
    st.dataframe(pd.DataFrame(listing["items"]), hide_index=True)
    by_id = {u["id"]: u for u in listing["items"]}
    target_id = st.selectbox(
        "修改使用者", list(by_id), format_func=lambda i: f"{i}：{by_id[i]['email']}"
    )
    target = by_id[target_id]
    with st.form(f"edit-user-{target_id}"):
        role = st.selectbox("角色", ROLES, index=ROLES.index(target["role"]))
        is_active = st.toggle("啟用", value=target["is_active"])
        save_user = st.form_submit_button("儲存使用者")
        st.caption(
            "變更立即對 API 生效；該使用者要重新登入，Streamlit 畫面才會顯示新的權限控制項。"
        )
    if save_user:
        edited = {"role": role, "is_active": is_active}
        changes = {k: v for k, v in edited.items() if v != target[k]}
        if not changes:
            st.info("沒有變更。")
        elif ui.call(api.admin_update_user, target_id, changes):
            ui.flash(f"已更新 {target['email']}：{changes}")

st.header("Service threshold")
services = ui.call(api.list_services) or []
with st.form("thresholds"):
    thresholds = {
        s["id"]: st.number_input(
            f"{s['display_name']}（{s['slug']}）", min_value=0, step=1, value=s["alert_threshold"]
        )
        for s in services
    }
    save_thresholds = st.form_submit_button("儲存 threshold")
if save_thresholds:
    changed = {
        s["id"]: int(thresholds[s["id"]])
        for s in services
        if int(thresholds[s["id"]]) != s["alert_threshold"]
    }
    if not changed:
        st.info("沒有變更。")
    elif all([ui.call(api.update_threshold, i, v) for i, v in changed.items()]):
        ui.flash("已更新 threshold；Generator 下一次重新載入（最多 5 秒）後套用到新事件。")

st.header("系統日誌")
columns = st.columns(4)
level = columns[0].selectbox("Level", LEVELS, format_func=everything)
event_type = columns[1].selectbox(
    "Event type",
    [None, *(ui.call(api.admin_event_types) or [])],
    format_func=everything,
    key="logs-event-type",
)
log_range = columns[2].selectbox("時間範圍", list(LOG_RANGES), key="logs-range")
logs_page = int(
    columns[3].number_input("頁碼", min_value=1, max_value=10_000, value=1, key="logs-page")
)
span = LOG_RANGES[log_range]
logs = ui.call(
    api.admin_logs,
    {
        "level": level,
        "event_type": event_type,
        "start": (datetime.now(UTC) - span).isoformat() if span else None,
        "page": logs_page,
        "page_size": 50,
    },
)
if logs:
    st.caption(f"共 {logs['total']} 筆，新到舊；時間皆為 UTC。")
    if logs["items"]:
        frame = pd.DataFrame(logs["items"])
        frame["context"] = frame["context"].map(lambda c: None if c is None else json.dumps(c))
        st.dataframe(frame, hide_index=True, width="stretch")

st.header("歷史查詢")
st.caption("已寫入 MariaDB 的 records；與 Records 頁同一組篩選。")
history = ui.call(api.list_records, {**ui.filters(services, key="admin-history"), "page_size": 50})
if history:
    st.caption(f"共 {history['total']} 筆，顯示最新 {len(history['items'])} 筆。")
    if history["items"]:
        st.dataframe(pd.DataFrame(history["items"]), hide_index=True, width="stretch")
