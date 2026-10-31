import os
from typing import Any, Literal
from urllib.parse import urljoin

import requests

API_BASE_URL = os.environ["API_BASE_URL"]
TIMEOUT_SECONDS = 5
# An import of 10,000 rows or an export of 50,000 takes seconds, not milliseconds.
TRANSFER_TIMEOUT_SECONDS = 60

# Codes a user can act on; any other error shows the backend's own message.
_MESSAGES = {
    "UNAUTHORIZED": "帳號或密碼錯誤，或登入已失效。",
    "TOKEN_EXPIRED": "登入已過期，請重新登入。",
    "FORBIDDEN": "你的角色不能執行這個操作。",
    "SERVICE_UNAVAILABLE": "後端暫時無法服務（資料庫不可用），請稍後再試。",
}


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        # Import row errors: [{"row", "field", "message"}].
        self.details = details


def describe(error: ApiError) -> str:
    return _MESSAGES.get(error.code, error.message)


def _check(response: requests.Response) -> requests.Response:
    if response.ok:
        return response
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
        details = error.get("details")
    except (ValueError, KeyError, TypeError, AttributeError):
        code, message, details = "HTTP_ERROR", f"HTTP {response.status_code}", None
    raise ApiError(response.status_code, code, message, details)


def _json(response: requests.Response) -> Any:
    return _check(response).json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _get(token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(
        f"{API_BASE_URL}{path}", params=params, headers=_auth(token), timeout=TIMEOUT_SECONDS
    )
    return _json(response)


def login(email: str, password: str) -> str:
    response = requests.post(
        f"{API_BASE_URL}/auth/token",
        data={"username": email, "password": password},
        timeout=TIMEOUT_SECONDS,
    )
    return _json(response)["access_token"]


def me(token: str) -> dict[str, Any]:
    return _get(token, "/auth/me")


def list_services(token: str) -> list[dict[str, Any]]:
    return _get(token, "/services")


def list_records(token: str, params: dict[str, Any]) -> dict[str, Any]:
    return _get(token, "/records", params)


def create_record(token: str, record: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{API_BASE_URL}/records", json=record, headers=_auth(token), timeout=TIMEOUT_SECONDS
    )
    return _json(response)


def update_record(token: str, record_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    response = requests.patch(
        f"{API_BASE_URL}/records/{record_id}",
        json=changes,
        headers=_auth(token),
        timeout=TIMEOUT_SECONDS,
    )
    return _json(response)


def delete_record(token: str, record_id: int) -> bool:
    _check(
        requests.delete(
            f"{API_BASE_URL}/records/{record_id}", headers=_auth(token), timeout=TIMEOUT_SECONDS
        )
    )
    return True


def import_file(token: str, filename: str, content: bytes) -> dict[str, Any]:
    response = requests.post(
        f"{API_BASE_URL}/imports",
        files={"file": (filename, content)},
        headers=_auth(token),
        timeout=TRANSFER_TIMEOUT_SECONDS,
    )
    return _json(response)


def analytics(
    token: str, kind: Literal["summary", "categories", "trends"], params: dict[str, Any]
) -> Any:
    return _get(token, f"/analytics/{kind}", params)


def export_xlsx(token: str, params: dict[str, Any]) -> bytes:
    response = requests.get(
        f"{API_BASE_URL}/analytics/export",
        params=params,
        headers=_auth(token),
        timeout=TRANSFER_TIMEOUT_SECONDS,
    )
    return _check(response).content


def get_readiness() -> dict[str, Any]:
    # /health/* sits at the server root, outside the /api/v1 prefix.
    response = requests.get(urljoin(API_BASE_URL, "/health/ready"), timeout=TIMEOUT_SECONDS)
    # 503 DEGRADED carries the same JSON body as 200 HEALTHY.
    if response.status_code not in (200, 503):
        response.raise_for_status()
    return response.json()
