import requests
import streamlit as st

from lib import api, session

st.set_page_config(page_title="API Traffic Monitoring")
st.title("API Traffic Monitoring")

if notice := session.take_notice():
    st.warning(notice)

user = session.current_user()
if user is None:
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("登入")
    if submitted:
        try:
            token = api.login(email, password)
            session.sign_in(token, api.me(token))
        except api.ApiError as error:
            st.error(api.describe(error))
        except requests.RequestException as exc:
            st.error(f"無法連到後端：{type(exc).__name__}")
        else:
            st.rerun()
else:
    st.success(f"已登入：{user['email']}（{user['role']}）")
    st.page_link("pages/1_Live_Monitor.py", label="前往 Live Monitor")
    if st.button("登出"):
        session.sign_out()
        st.rerun()

try:
    readiness = api.get_readiness()
except requests.RequestException as exc:
    st.error(f"Backend unreachable: {type(exc).__name__}")
else:
    state = readiness["state"]
    if state == "HEALTHY":
        st.caption(f"Backend state: {state}")
    else:
        st.warning(f"Backend state: {state}")
