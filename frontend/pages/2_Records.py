import math
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from lib import api, session, ui

SORTS = {
    "-observed_at": "observed_at 新 → 舊",
    "observed_at": "observed_at 舊 → 新",
    "-value": "value 大 → 小",
    "value": "value 小 → 大",
    "-persisted_at": "persisted_at 新 → 舊",
    "persisted_at": "persisted_at 舊 → 新",
}
COLUMNS = [
    "id",
    "service",
    "title",
    "value",
    "is_anomaly",
    "threshold_applied",
    "observed_at",
    "source",
    "created_by",
    "persisted_at",
    "import_batch_id",
    "event_id",
]

st.set_page_config(page_title="Records", layout="wide")
session.require_login()
user = session.current_user() or {}
# Hiding controls is convenience only; the API enforces roles and ownership either way.
can_write = user.get("role") in ("ADMIN", "USER")

st.title("Records")
ui.show_flash()
services = ui.call(api.list_services) or []
slug_by_id = {s["id"]: s["slug"] for s in services}

params = ui.filters(services, key="records")
left, middle, right = st.columns(3)
sort = left.selectbox("排序", list(SORTS), format_func=SORTS.__getitem__)
page_size = middle.selectbox("每頁筆數", [20, 50, 100])
page = int(right.number_input("頁碼", min_value=1, max_value=10_000, value=1, step=1))

listing = ui.call(api.list_records, {**params, "sort": sort, "page": page, "page_size": page_size})
items = listing["items"] if listing else []
if listing:
    pages = max(1, math.ceil(listing["total"] / page_size))
    st.caption(f"共 {listing['total']} 筆，第 {page} / {pages} 頁；時間皆為 UTC。")
    if items:
        frame = pd.DataFrame(items)
        frame["service"] = frame["service_id"].map(slug_by_id)
        st.dataframe(frame[COLUMNS], hide_index=True, width="stretch")
    else:
        st.info("沒有符合條件的資料。")

if can_write:
    with st.expander("新增 record"):
        with st.form("create-record", clear_on_submit=True):
            slug = st.selectbox("Service", [s["slug"] for s in services if s["is_active"]])
            title = st.text_input("Title", max_chars=255)
            value = st.number_input("Value（requests / s）", min_value=0, step=1)
            observed_at = st.text_input(
                "observed_at（ISO 8601，必須含時區）",
                value=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
            create = st.form_submit_button("新增")
        if create:
            record = {
                "service_slug": slug,
                "title": title,
                "value": int(value),
                "observed_at": observed_at,
            }
            if created := ui.call(api.create_record, record):
                ui.flash(f"已新增 record {created['id']}（anomaly：{created['is_anomaly']}）")

    # USER may modify only rows they created; ADMIN any row (ADR-002).
    editable = [
        r for r in items if user.get("role") == "ADMIN" or r["created_by"] == user.get("id")
    ]
    with st.expander(f"編輯 / 刪除（本頁可修改 {len(editable)} 筆）"):
        if not editable:
            st.caption("本頁沒有你可以修改的資料。")
        else:
            by_id = {r["id"]: r for r in editable}
            record_id = st.selectbox(
                "Record",
                list(by_id),
                format_func=lambda i: (
                    f"{i}：{slug_by_id.get(by_id[i]['service_id'])} "
                    f"{by_id[i]['observed_at']} value={by_id[i]['value']}"
                ),
            )
            current = by_id[record_id]
            with st.form(f"edit-record-{record_id}"):
                title = st.text_input("Title", value=current["title"], max_chars=255)
                value = st.number_input("Value", min_value=0, step=1, value=current["value"])
                observed_at = st.text_input(
                    "observed_at（ISO 8601，含時區）", current["observed_at"]
                )
                save = st.form_submit_button("儲存變更")
            if save:
                edited = {"title": title, "value": int(value), "observed_at": observed_at}
                changes = {k: v for k, v in edited.items() if v != current[k]}
                if not changes:
                    st.info("沒有變更。")
                elif updated := ui.call(api.update_record, record_id, changes):
                    ui.flash(
                        f"已更新 record {record_id}（anomaly：{updated['is_anomaly']}，"
                        f"沿用 threshold {updated['threshold_applied']}）"
                    )
            confirm = st.checkbox(f"確認刪除 record {record_id}", key=f"confirm-{record_id}")
            if st.button("刪除", disabled=not confirm, type="primary") and ui.call(
                api.delete_record, record_id
            ):
                ui.flash(f"已刪除 record {record_id}。")

    with st.expander("匯入 CSV / JSON"):
        st.caption(
            "CSV 標頭：`event_id,service_slug,title,value,observed_at`；JSON 為同欄位的物件陣列。"
            "全部列都通過驗證才寫入，任何一列錯誤就整批不寫入。event_id 可留空。"
        )
        upload = st.file_uploader("檔案", type=["csv", "json"])
        if upload is not None and st.button("匯入"):
            batch = ui.call(api.import_file, upload.name, upload.getvalue())
            if batch:
                ui.flash(
                    f"{batch['status']}：batch {batch['id']}，共 {batch['total_count']} 筆，"
                    f"成功 {batch['success_count']}，失敗 {batch['failure_count']}。"
                )
else:
    st.caption("VIEWER 只能查詢；新增、編輯、刪除與匯入需要 USER 或 ADMIN。")
