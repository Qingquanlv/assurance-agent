"""Administrator authentication for generated performance scenarios."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def acquire_admin_token(client: Any) -> dict[str, str]:
    username = os.environ.get("AA_ADMIN_USERNAME", "admin")
    password = os.environ.get("AA_ADMIN_PASSWORD", "123456")
    with client.post(
        "/api/v1/base/access_token",
        json={"username": username, "password": password},
        name="POST /api/v1/base/access_token",
        catch_response=True,
    ) as response:
        if response.status_code >= 400:
            response.failure(f"administrator login returned HTTP {response.status_code}")
            raise RuntimeError("unable to obtain administrator performance token")
        body: Mapping[str, Any] = response.json()
        data = body.get("data", body)
        token = data.get("access_token") if isinstance(data, Mapping) else None
        if not isinstance(token, str) or not token:
            response.failure("administrator login response did not contain a token")
            raise RuntimeError("unable to obtain administrator performance token")
        return {"token": token}
