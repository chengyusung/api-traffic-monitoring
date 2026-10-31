import os
from typing import Any
from urllib.parse import urljoin

import requests

API_BASE_URL = os.environ["API_BASE_URL"]
TIMEOUT_SECONDS = 5


def get_readiness() -> dict[str, Any]:
    # /health/* sits at the server root, outside the /api/v1 prefix.
    response = requests.get(urljoin(API_BASE_URL, "/health/ready"), timeout=TIMEOUT_SECONDS)
    # 503 DEGRADED carries the same JSON body as 200 HEALTHY.
    if response.status_code not in (200, 503):
        response.raise_for_status()
    return response.json()
