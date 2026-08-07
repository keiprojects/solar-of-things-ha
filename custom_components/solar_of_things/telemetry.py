"""Telemetry compatibility helpers for Solar of Things devices.

Siseli protocol profiles do not all expose the same attribute keys.  This
module requests the common aliases and normalises them to the stable keys used
by the Home Assistant sensor platform.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .const import API_TIME_SERIES


TELEMETRY_KEYS = [
    # PV power: legacy integrations and newer protocol profiles.
    "pvInputPower",
    "pvPower",
    "generationPower",
    # AC output / home load.
    "acOutputActivePower",
    "outputActivePower",
    # Battery telemetry.
    "batteryDischargeCurrent",
    "batteryChargingCurrent",
    "batteryVoltage",
    "batterySOC",
    "batteryCapacity",
    # Grid import/export.
    "feedInPower",
    "mainsPower",
    "mainsCurrentFlowDirection",
]


def _as_float(value: Any) -> float | None:
    """Return a numeric value, or ``None`` when it cannot be converted."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(values: dict[str, Any], *keys: str) -> float | None:
    """Return the first present numeric key, preserving valid zero values."""
    for key in keys:
        value = _as_float(values.get(key))
        if value is not None:
            return value
    return None


def _normalise_telemetry(values: dict[str, Any]) -> dict[str, Any]:
    """Normalise protocol-specific fields to the integration sensor keys."""
    normalised: dict[str, Any] = {}

    # Legacy pvInputPower is already treated as watts by the integration.
    pv_watts = _first_float(values, "pvInputPower")
    if pv_watts is None:
        pv_kw = _first_float(values, "pvPower", "generationPower")
        if pv_kw is not None:
            pv_watts = pv_kw * 1000.0
    if pv_watts is not None:
        normalised["pvInputPower"] = pv_watts

    # Both output power aliases are reported in kW by Siseli.
    output_kw = _first_float(values, "acOutputActivePower", "outputActivePower")
    if output_kw is not None:
        output_watts = output_kw * 1000.0
        normalised["acOutputActivePower"] = output_watts
        normalised["loadPower"] = output_watts

    voltage = _first_float(values, "batteryVoltage")
    charge = _first_float(values, "batteryChargingCurrent")
    discharge = _first_float(values, "batteryDischargeCurrent")
    soc = _first_float(values, "batterySOC", "batteryCapacity")

    if voltage is not None:
        normalised["batteryVoltage"] = voltage
    if charge is not None:
        normalised["batteryChargingCurrent"] = charge
    if discharge is not None:
        normalised["batteryDischargeCurrent"] = discharge
    if soc is not None:
        normalised["batterySOC"] = soc

    # Positive batteryPower means discharge; negative means charging.
    if voltage is not None and (charge is not None or discharge is not None):
        normalised["batteryPower"] = (
            (discharge or 0.0) - (charge or 0.0)
        ) * voltage

    mains_kw = _first_float(values, "mainsPower")
    direction = values.get("mainsCurrentFlowDirection")

    if mains_kw is not None:
        mains_watts = abs(mains_kw) * 1000.0
        if direction == "-" or (direction not in ("+", "-") and mains_kw < 0):
            normalised["gridPower"] = 0.0
            normalised["feedInPower"] = mains_watts
        else:
            normalised["gridPower"] = mains_watts
            normalised["feedInPower"] = 0.0
    else:
        # Retain the legacy feed-in field when this protocol has no mainsPower.
        feed_in = _first_float(values, "feedInPower")
        if feed_in is not None:
            normalised["feedInPower"] = max(0.0, feed_in)

        # Estimate import only when the required readings are available.
        pv = _as_float(normalised.get("pvInputPower"))
        load = _as_float(normalised.get("loadPower"))
        battery = _as_float(normalised.get("batteryPower"))
        if load is not None and pv is not None:
            normalised["gridPower"] = max(
                0.0,
                load - pv - (battery or 0.0) + (feed_in or 0.0),
            )

    return normalised


def fetch_latest_telemetry(api: Any, device_id: str) -> dict[str, Any]:
    """Fetch and normalise the latest readings for one device."""
    end_time = api._now()
    start_time = end_time - timedelta(hours=1)

    data = api._post(
        API_TIME_SERIES,
        {
            "deviceId": device_id,
            "count": 2000,
            "page": 1,
            "fromTime": api._format_time(start_time),
            "toTime": api._format_time(end_time),
            "orderByTimeAsc": True,
            "keys": TELEMETRY_KEYS,
        },
    )

    if data.get("code") not in (0, None):
        raise RuntimeError(
            f"Timeseries error code={data.get('code')} "
            f"message={data.get('message')}"
        )

    payload = (data.get("data") or {}).get("payload") or {}
    fields = payload.get("fields") or {}

    latest_values: dict[str, Any] = {}
    for key, samples in fields.items():
        if isinstance(samples, list) and samples:
            latest_values[key] = samples[-1]

    return _normalise_telemetry(latest_values)
