import time
from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from lib import session
from lib.ws import ReceiverState, merge_window

WINDOW = timedelta(seconds=120)
STALE_AFTER_S = 10
CONNECTION_LABELS = {
    ReceiverState.CONNECTING: "connecting（連線中）",
    ReceiverState.CONNECTED: "connected（已連線）",
    ReceiverState.RECONNECTING: "reconnecting（重新連線中，退避 1 → 30 秒）",
    ReceiverState.UNAUTHORIZED: "unauthorized（登入已失效）",
    ReceiverState.STOPPED: "stopped（已停止）",
}

st.set_page_config(page_title="Live Monitor", layout="wide")
session.require_login()
receiver = session.receiver()

st.title("Live Monitor")
st.info(
    "**即時（尚未持久化）**：這裡顯示的是推送當下的觀測值，還沒寫進 MariaDB。"
    "Writer 約每 5 秒批次寫入，歷史查詢會晚一點才看得到；斷線期間的資料不會補送。"
)


@st.fragment(run_every=1)
def live() -> None:
    snapshot = receiver.snapshot()
    if snapshot.state is ReceiverState.UNAUTHORIZED:
        session.sign_out(notice="登入已失效（token 過期或帳號已停用），請重新登入。")
        st.rerun(scope="app")

    window = session.live_window()
    merge_window(window, snapshot.events, WINDOW)

    connection = CONNECTION_LABELS[snapshot.state]
    last = snapshot.last_message_at
    if snapshot.state is ReceiverState.CONNECTED and (
        last is None or time.monotonic() - last > STALE_AFTER_S
    ):
        connection = f"stale（超過 {STALE_AFTER_S} 秒沒有訊息）"
    system = snapshot.status or {}
    newest = max(window.values(), key=lambda e: e["observed_at"], default=None)

    st.markdown(
        f"- 連線狀態：**{connection}**\n"
        f"- 最後事件時間（UTC）：{newest['observed_at'] if newest else '尚未收到'}\n"
        f"- 系統狀態：**{system.get('state', '未知')}**，"
        f"未提交事件 {system.get('total_uncommitted', '–')} 筆（狀態改變時更新）\n"
        f"- 近 {int(WINDOW.total_seconds())} 秒收到 {len(window)} 筆事件"
    )
    if not window:
        st.caption("等待第一筆事件…")
        return

    frame = pd.DataFrame(window.values())
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    # A :T tooltip is formatted in the browser's zone; the string keeps the UTC the page states.
    frame["observed_at_utc"] = frame["observed_at"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    latest = frame.sort_values("observed_at").groupby("service_slug").tail(1)

    for row in latest.sort_values("service_slug").itertuples():
        flag = " :red[**異常**]" if row.is_anomaly else ""
        st.markdown(f"{row.title}：**{row.value}**（threshold {row.threshold_applied}）{flag}")

    x = alt.X("observed_at:T", title="observed_at (UTC)", scale=alt.Scale(type="utc"))
    y = alt.Y("value:Q", title="requests / s")
    lines = alt.Chart(frame).mark_line().encode(x=x, y=y, color=alt.Color("service_slug:N"))
    anomalies = (
        alt.Chart(frame[frame["is_anomaly"]])
        .mark_point(color="red", filled=True, size=80)
        .encode(x=x, y=y, tooltip=["service_slug", "value", "threshold_applied", "observed_at_utc"])
    )
    st.altair_chart(lines + anomalies, width="stretch")

    bars = (
        alt.Chart(latest)
        .mark_bar()
        .encode(
            x=alt.X("service_slug:N", title="service"),
            y=alt.Y("value:Q", title="latest requests / s"),
            color=alt.condition(alt.datum.is_anomaly, alt.value("red"), alt.value("steelblue")),
            tooltip=["title", "value", "threshold_applied", "observed_at_utc"],
        )
    )
    st.altair_chart(bars, width="stretch")


live()
