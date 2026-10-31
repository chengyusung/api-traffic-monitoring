"""Per-browser-session state. Only the script thread calls these."""

import os
from typing import Any

import streamlit as st

from lib.ws import Event, Receiver

API_WS_URL = os.environ["API_WS_URL"]


def current_user() -> dict[str, Any] | None:
    return st.session_state.get("user")


def sign_in(token: str, user: dict[str, Any]) -> None:
    sign_out()
    st.session_state["token"] = token
    st.session_state["user"] = user


def sign_out(notice: str | None = None) -> None:
    """Stop the receiver thread, then forget the token, the user and the live window."""
    receiver = st.session_state.pop("ws_receiver", None)
    if receiver is not None:
        receiver.stop()
    for key in ("token", "user", "live_window"):
        st.session_state.pop(key, None)
    if notice:
        st.session_state["notice"] = notice


def take_notice() -> str | None:
    return st.session_state.pop("notice", None)


def require_login() -> str:
    token = st.session_state.get("token")
    if token is None:
        st.switch_page("app.py")
    return token


def receiver() -> Receiver:
    """One receiver per session. Reruns reuse it; sign_out() or a crashed thread replaces it."""
    current = st.session_state.get("ws_receiver")
    if current is None or current.crashed:
        current = Receiver(API_WS_URL, require_login())
        current.start()
        st.session_state["ws_receiver"] = current
    return current


def live_window() -> dict[str, Event]:
    return st.session_state.setdefault("live_window", {})
