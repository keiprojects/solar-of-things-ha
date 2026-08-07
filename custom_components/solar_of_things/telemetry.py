"""Inverter-specific telemetry collection and normalisation.

This fork targets gather protocol version 493332949100363776 (protocol code
44).  The protocol advertises 162 state attributes.  Every advertised key is
requested and kept in ``raw`` while a small set of canonical values is derived
for the existing Home Assistant energy entities.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from .const import API_TIME_SERIES

PROTOCOL_SCHEMA: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("protocol_schema.json").read_text(encoding="utf-8")
)
TELEMETRY_KEYS: tuple[str, ...] = tuple(PROTOCOL_SCHEMA)

# Keep requests small enough for the Siseli history endpoint while still
# collecting the complete protocol on every coordinator refresh.
TELEMETRY_BATCH_SIZE = 40


def _chunks(values: tuple[str, ...], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _latest_sample(value: Any) -> Any:
    """Extract the latest scalar from the possible history response shapes."""
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

    # On this inverter's live data, outputActivePower tracks solar generation
    # rather than the household load despite its protocol label. Do not expose
    # it as load watts. The reliable load-side values are apparent power (VA)
    # and output load percentage.
    output_va = _first_float(raw, "outputApparentPower")
    if output_va is not None:
        canonical["loadApparentPower"] = output_va

    output_percent = _first_float(raw, "outputLoadPercent")
    if output_percent is not None:
        canonical["loadPercent"] = output_percent

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


def fetch_latest_telemetry(api: Any, device_id: str) -> dict[str, Any]:
    """Fetch every state attribute advertised by this inverter protocol."""
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

    return {
        "raw": raw,
        "canonical": _canonical_values(raw),
    }
