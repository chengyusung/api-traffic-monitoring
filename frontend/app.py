import requests
import streamlit as st

from lib.api import get_readiness

st.set_page_config(page_title="API Traffic Monitoring")
st.title("API Traffic Monitoring")

try:
    readiness = get_readiness()
except requests.RequestException as exc:
    st.error(f"Backend unreachable: {type(exc).__name__}")
else:
    state = readiness["state"]
    if state == "HEALTHY":
        st.success(f"Backend state: {state}")
    else:
        st.warning(f"Backend state: {state}")
