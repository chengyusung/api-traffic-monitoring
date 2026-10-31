import json

import altair as alt
import pandas as pd
import streamlit as st

from lib import api, session, ui

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
METRICS = {
    "sum": "總 requests（sum）",
    "avg_per_observation": "平均每筆觀測（avg）",
    "max": "最大值（max）",
    "count": "觀測筆數（count）",
    "anomaly_count": "異常筆數",
}


def shown(value: float | None, digits: int = 0) -> str:
    return "–" if value is None else f"{value:,.{digits}f}"


st.set_page_config(page_title="Analytics", layout="wide")
session.require_login()
st.title("Analytics")
st.caption("資料來自 MariaDB 已寫入的 records（不含尚未持久化的即時事件）；時間皆為 UTC。")

services = ui.call(api.list_services) or []
params = ui.filters(services, key="analytics")

summary = ui.call(api.analytics, "summary", params)
if summary:
    tiles = st.columns(6)
    tiles[0].metric("觀測筆數", shown(summary["count"]))
    tiles[1].metric("總 requests", shown(summary["sum"]))
    tiles[2].metric("平均每筆觀測", shown(summary["avg_per_observation"], 1))
    tiles[3].metric("最小值", shown(summary["min"]))
    tiles[4].metric("最大值", shown(summary["max"]))
    tiles[5].metric("異常筆數", shown(summary["anomaly_count"]))
    st.caption(
        "平均是每一筆「Service × 秒」觀測的平均值。混合多個 Service 時它不是整體 RPS；"
        "單一 Service 時才等於該 Service 的平均 RPS。沒有資料時顯示 –，不是 0。"
    )

st.subheader("各 Service")
categories = ui.call(api.analytics, "categories", params)
if categories:
    table = pd.DataFrame(categories).rename(columns={"avg_per_observation": "avg RPS"})
    st.dataframe(
        table[["service", "count", "sum", "avg RPS", "min", "max", "anomaly_count"]],
        hide_index=True,
        width="stretch",
    )
elif categories is not None:
    st.info("沒有符合條件的資料。")

st.subheader("趨勢")
left, right = st.columns(2)
bucket = left.radio("Bucket", ["minute", "hour"], horizontal=True)
metric = right.selectbox("指標", list(METRICS), format_func=METRICS.__getitem__)
points = ui.call(api.analytics, "trends", {**params, "bucket": bucket})
if points:
    frame = pd.DataFrame(points)
    frame["bucket_start"] = pd.to_datetime(frame["bucket_start"], utc=True)
    # Bars, not a line: a bucket without rows is absent and a line would draw through it.
    chart = (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X("bucket_start:T", title=f"{bucket} (UTC)"),
            y=alt.Y(f"{metric}:Q", title=METRICS[metric]),
            tooltip=["bucket_start:T", *METRICS],
        )
    )
    st.altair_chart(chart, width="stretch")
    st.caption("沒有資料的 bucket 不畫出來，也不補 0。")
elif points is not None:
    st.info("沒有符合條件的資料。")

st.subheader("Excel")
st.caption("匯出目前篩選條件下的明細（上限 50,000 列，超過請縮小範圍）。")
export_key = json.dumps(params, sort_keys=True)
if st.button("產生 Excel"):
    content = ui.call(api.export_xlsx, params)
    if content is not None:
        st.session_state["export"] = (export_key, content)
prepared = st.session_state.get("export")
if prepared and prepared[0] == export_key:
    st.download_button(
        "下載 traffic_records.xlsx", prepared[1], file_name="traffic_records.xlsx", mime=XLSX
    )
