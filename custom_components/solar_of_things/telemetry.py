"""Inverter-specific telemetry collection and normalisation.

This fork targets gather protocol version 493332949100363776 (protocol code
44). Normal polling uses Siseli's current-state endpoints instead of repeatedly
querying one hour of history. The history endpoint remains as a compatibility
fallback.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .const import (
    API_BASE_URL,
    API_LIVE_ENERGY_FLOW,
    API_LIVE_STATE,
    API_TIME_SERIES,
)

PROTOCOL_SCHEMA: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("protocol_schema.json").read_text(encoding="utf-8")
)
TELEMETRY_KEYS: tuple[str, ...] = tuple(PROTOCOL_SCHEMA)

# Used only when both live snapshot endpoints are unavailable.
TELEMETRY_BATCH_SIZE = 40


def _chunks(values: tuple[str, ...], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _latest_sample(value: Any) -> Any:
    """Extract a scalar from live attribute objects or history samples."""
    if isinstance(value, list):
        if not value:
            return None
        value = value[-1]

    if isinstance(value, dict):
        for key in ("value", "v", "data"):
            if key in value:
                return value[key]
    return value


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(values: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _as_float(values.get(key))
        if parsed is not None:
            return parsed
    return None


def _canonical_values(raw: dict[str, Any]) -> dict[str, Any]:
    """Build stable, dashboard-friendly values from protocol-code-44 fields."""
    canonical: dict[str, Any] = {}

    pv_kw = _first_float(raw, "pvPower", "generationPower")
    if pv_kw is not None:
        canonical["pvInputPower"] = pv_kw * 1000.0

    output_kw = _first_float(raw, "outputActivePower", "acOutputActivePower")
    if output_kw is not None:
        output_watts = output_kw * 1000.0
        canonical["acOutputActivePower"] = output_watts
        canonical["loadPower"] = output_watts

    voltage = _first_float(raw, "batteryVoltage")
    charge = _first_float(raw, "batteryChargingCurrent", "bmsChargingCurrent")
    discharge = _first_float(raw, "batteryDischargeCurrent", "bmsDischargeCurrent")
    soc = _first_float(raw, "batteryCapacity", "bmsCurrentSOC")

    if voltage is not None:
        canonical["batteryVoltage"] = voltage
    if charge is not None:
        canonical["batteryChargingCurrent"] = charge
    if discharge is not None:
        canonical["batteryDischargeCurrent"] = discharge
    if soc is not None:
        canonical["batterySOC"] = soc

    if voltage is not None and (charge is not None or discharge is not None):
        canonical["batteryPower"] = (
            (discharge or 0.0) - (charge or 0.0)
        ) * voltage

    mains_kw = _first_float(raw, "mainsPower")
    direction = raw.get("mainsCurrentFlowDirection")
    if mains_kw is not None:
        mains_watts = abs(mains_kw) * 1000.0
        if direction == "-" or (direction not in ("+", "-") and mains_kw < 0):
            canonical["gridPower"] = 0.0
            canonical["feedInPower"] = mains_watts
        else:
            canonical["gridPower"] = mains_watts
            canonical["feedInPower"] = 0.0

    return canonical


def _live_get(api: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET a Siseli live endpoint using the exact web-client request shape.

    The live state endpoint does not use IOT-Open-AppID request signing. The
    official Solar of Things web client sends only the current IOT token and
    time-zone headers for this request.
    """
    api._ensure_token_valid()

    def _headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "IOT-Token": api.access_token,
            "IOT-Time-Zone": getattr(api, "_time_zone", "Asia/Manila"),
        }

    response = api.session.get(
        f"{API_BASE_URL}{path}",
        params=params,
        headers=_headers(),
        timeout=8,
    )

    if response.status_code == 401:
        # Force the API client's existing refresh/re-login strategy, then retry
        # once with the new token.
        api._access_expires = datetime.now(timezone.utc)
        api._ensure_token_valid()
        response = api.session.get(
            f"{API_BASE_URL}{path}",
            params=params,
            headers=_headers(),
            timeout=8,
        )

    response.raise_for_status()
    data = response.json()
    if data.get("code") not in (0, "0", None):
        raise RuntimeError(
            f"Live telemetry error code={data.get('code')} "
            f"message={data.get('message')} endpoint={path}"
        )
    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}


def _flatten_live_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Flatten a live response and retain its cloud sample timestamp.

    Energy-flow node values intentionally override duplicate values from the
    full state object. The Solar of Things energy-flow UI can return fresher
    node values than the larger cached state snapshot.
    """
    raw: dict[str, Any] = {}

    state = payload.get("deviceAttributeState")
    if not isinstance(state, dict):
        state = payload

    sample_time = state.get("time") if isinstance(state, dict) else None
    if sample_time is None:
        sample_time = payload.get("time")

    fields = state.get("fields") if isinstance(state, dict) else None
    if isinstance(fields, dict):
        for key, item in fields.items():
            value = _latest_sample(item)
            if value is not None:
                raw[key] = value

    # Prefer the energy-flow node values over duplicate fields. This is
    # important for the frequently changing PV/grid/battery/load values shown
    # by the live Solar of Things energy-flow screen.
    for node_name in (
        "pvPanelFlow",
        "gridFlow",
        "batteryFlow",
        "loadFlow",
        "generatorFlow",
        "upsFlow",
        "ctFlow",
    ):
        node = payload.get(node_name)
        if not isinstance(node, dict):
            continue

        node_key = node.get("key")
        node_value = _latest_sample(node.get("value"))
        if node_key and node_value is not None:
            raw[str(node_key)] = node_value

        extras = node.get("extraValues")
        if isinstance(extras, list):
            for extra in extras:
                if not isinstance(extra, dict):
                    continue
                key = extra.get("key")
                value = _latest_sample(extra)
                if key and value is not None:
                    raw[str(key)] = value

    return raw, str(sample_time) if sample_time is not None else None


def _fetch_live_telemetry(
    api: Any, device_id: str
) -> tuple[dict[str, Any], str, str | None]:
    """Fetch one current snapshot, preferring the energy-flow endpoint."""
    errors: list[str] = []
    params = {"deviceId": device_id, "dataSource": 1}

    for path in (API_LIVE_ENERGY_FLOW, API_LIVE_STATE):
        try:
            payload = _live_get(api, path, params)
            raw, sample_time = _flatten_live_payload(payload)
            if raw:
                return raw, path, sample_time
            errors.append(f"{path}: empty fields")
        except Exception as err:
            errors.append(f"{path}: {err}")

    raise RuntimeError("; ".join(errors) or "No live telemetry returned")


def _fetch_history_fallback(api: Any, device_id: str) -> dict[str, Any]:
    """Fallback to the older batched history endpoint."""
    end_time = api._now()
    start_time = end_time - timedelta(hours=1)
    raw: dict[str, Any] = {}
    errors: list[str] = []

    for keys in _chunks(TELEMETRY_KEYS, TELEMETRY_BATCH_SIZE):
        try:
            data = api._post(
                API_TIME_SERIES,
                {
                    "deviceId": device_id,
                    "count": 2000,
                    "page": 1,
                    "fromTime": api._format_time(start_time),
                    "toTime": api._format_time(end_time),
                    "orderByTimeAsc": True,
                    "keys": list(keys),
                },
            )

            if data.get("code") not in (0, None, "0"):
                raise RuntimeError(
                    f"code={data.get('code')} message={data.get('message')}"
                )

            payload = (data.get("data") or {}).get("payload") or {}
            fields = payload.get("fields") or {}
            if not isinstance(fields, dict):
                continue

            for key, samples in fields.items():
                latest = _latest_sample(samples)
                if latest is not None:
                    raw[key] = latest
        except Exception as err:
            errors.append(f"{list(keys)!r}: {err}")

    if not raw and errors:
        raise RuntimeError("All telemetry batches failed: " + "; ".join(errors))
    return raw


def fetch_latest_telemetry(api: Any, device_id: str) -> dict[str, Any]:
    """Fetch current inverter telemetry with automatic history fallback."""
    polled_at = datetime.now(timezone.utc).isoformat()

    try:
        raw, source, sample_time = _fetch_live_telemetry(api, device_id)
        return {
            "raw": raw,
            "canonical": _canonical_values(raw),
            "source": "live",
            "live_endpoint": source,
            "cloud_sample_time": sample_time,
            "polled_at": polled_at,
        }
    except Exception as live_error:
        raw = _fetch_history_fallback(api, device_id)
        return {
            "raw": raw,
            "canonical": _canonical_values(raw),
            "source": "history_fallback",
            "live_error": str(live_error),
            "cloud_sample_time": None,
            "polled_at": polled_at,
        }
