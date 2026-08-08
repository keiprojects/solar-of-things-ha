"""Verified Solar of Things inverter control writes.

The request shape in this module mirrors the live solar.siseli.com Control UI:

POST /apis/remote/device/config/write?deviceId=<device_id>
JSON: {"id": "<device_id>", "key": "<setting_key>", "value": "<value>"}

The write endpoint uses the normal authenticated IOT-Token session.  It does
not use the IOT-Open signing headers used by the login endpoint.
"""
from __future__ import annotations

from typing import Any

from .const import API_BASE_URL, API_SETTINGS_SET


def write_setting(api: Any, device_id: str, key: str, value: Any) -> None:
    """Write one inverter setting using the exact Siseli portal request format."""
    api._ensure_token_valid()

    url = f"{API_BASE_URL}{API_SETTINGS_SET}?deviceId={device_id}"
    payload = {
        "id": device_id,
        "key": key,
        "value": str(value),
    }

    resp = api.session.post(url, json=payload, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") not in (0, "0", None):
        raise RuntimeError(
            f"Settings write error code={data.get('code')} "
            f"message={data.get('message')} (key={key})"
        )
