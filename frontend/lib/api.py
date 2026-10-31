import os
from typing import Any
from urllib.parse import urljoin

import requests

API_BASE_URL = os.environ["API_BASE_URL"]
TIMEOUT_SECONDS = 5

# Codes a user can act on; any other error shows the backend's own message.
_MESSAGES = {
    "UNAUTHORIZED": "帳號或密碼錯誤，或登入已失效。",
    "TOKEN_EXPIRED": "登入已過期，請重新登入。",
    "FORBIDDEN": "你的角色不能執行這個操作。",
    "SERVICE_UNAVAILABLE": "後端暫時無法服務（資料庫不可用），請稍後再試。",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def describe(error: ApiError) -> str:
    return _MESSAGES.get(error.code, error.message)


def _json(response: requests.Response) -> Any:
    if response.ok:
        return response.json()
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        code, message = "HTTP_ERROR", f"HTTP {response.status_code}"
    raise ApiError(response.status_code, code, message)


def login(email: str, password: str) -> str:
    response = requests.post(
        f"{API_BASE_URL}/auth/token",
        data={"username": email, "password": password},
        timeout=TIMEOUT_SECONDS,
    )
    return _json(response)["access_token"]


def me(token: str) -> dict[str, Any]:
    response = requests.get(
        f"{API_BASE_URL}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
    )
    return _json(response)


def get_readiness() -> dict[str, Any]:
    # /health/* sits at the server root, outside the /api/v1 prefix.
    response = requests.get(urljoin(API_BASE_URL, "/health/ready"), timeout=TIMEOUT_SECONDS)
    # 503 DEGRADED carries the same JSON body as 200 HEALTHY.
    if response.status_code not in (200, 503):
        response.raise_for_status()
    return response.json()
