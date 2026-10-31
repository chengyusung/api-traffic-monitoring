"""Widgets the Records and Analytics pages share. Script thread only."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd
import requests
import streamlit as st

from lib import api, session

EXPIRED = "登入已失效（token 過期或帳號已停用），請重新登入。"
RANGES: dict[str, timedelta | None] = {
    "全部": None,
    "最近 15 分鐘": timedelta(minutes=15),
    "最近 1 小時": timedelta(hours=1),
    "最近 24 小時": timedelta(hours=24),
    "最近 7 天": timedelta(days=7),
    "自訂日期（UTC）": None,
}
ANOMALY = {None: "全部", True: "只看異常", False: "只看正常"}


def call[T](fn: Callable[..., T], *args: Any) -> T | None:
    """Call lib.api with the session token; on failure show the backend's message and return None.

    A 401 signs the user out and returns to the login page.
    """
    try:
        return fn(session.require_login(), *args)
    except api.ApiError as error:
        if error.status == 401:
            session.sign_out(notice=EXPIRED)
            st.switch_page("app.py")
        text = api.describe(error)
        if text != error.message:
            text = f"{text}（{error.code}：{error.message}）"
        st.error(text)
        if error.details:
            st.dataframe(pd.DataFrame(error.details), hide_index=True)
    except requests.RequestException as exc:
        st.error(f"無法連到後端：{type(exc).__name__}")
    return None


def flash(message: str) -> None:
    """Rerun so the page shows fresh data, keeping one success message for the next run."""
    st.session_state["flash"] = message
    st.rerun()


def show_flash() -> None:
    if message := st.session_state.pop("flash", None):
        st.success(message)


def _label(value: Any) -> str:
    return "全部" if value is None else str(value)


def filters(services: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """The records list filters as query params; the backend validates them."""
    columns = st.columns(5)
    service = columns[0].selectbox(
        "Service", [None, *(s["slug"] for s in services)], format_func=_label, key=f"{key}-service"
    )
    source = columns[1].selectbox(
        "Source", [None, "MANUAL", "IMPORT", "SIMULATOR"], format_func=_label, key=f"{key}-source"
    )
    anomaly = columns[2].selectbox(
        "異常", list(ANOMALY), format_func=ANOMALY.__getitem__, key=f"{key}-anomaly"
    )
    window = columns[3].selectbox("時間範圍", list(RANGES), key=f"{key}-range")
    q = columns[4].text_input("Title 包含", key=f"{key}-q", max_chars=255)

    start = end = None
    if window == "自訂日期（UTC）":
        today = datetime.now(UTC).date()
        picked = st.date_input("日期區間（UTC，含頭含尾）", value=(today, today), key=f"{key}-days")
        days = picked if isinstance(picked, tuple) else (picked,)
        if days and isinstance(days[0], date):
            start = datetime.combine(days[0], time(), UTC)
            end = datetime.combine(days[-1], time(), UTC) + timedelta(days=1)
    elif (span := RANGES[window]) is not None:
        start = datetime.now(UTC) - span
    return {
        "service": service,
        "source": source,
        "is_anomaly": None if anomaly is None else str(anomaly).lower(),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "q": q or None,
    }
