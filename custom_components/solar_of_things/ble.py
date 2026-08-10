"""Local Bluetooth transport for Solar of Things / WIFI-RELAB collectors.

The confirmed PowMr POW-HVM6.2KP / HPVINV02 family uses the Solar Plug
H-command serial protocol. Read requests are plain ASCII terminated by CR
(e.g. HGRID, HOP, HBAT, HPV); they are not Voltronic QPIGS frames.

WIFI-RELAB exposes the inverter UART through the FEE7 BLE service. We prefer
the transparent write-without-response/notify pair FEC7/FEC8 and fall back to
the write/indicate pair FED5/FED6 used by Solar of Things Proximal Monitoring.

Only read-only H commands are sent here. Configuration/write commands are
intentionally not implemented by this transport.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

BLE_SERVICE_UUID = "0000fee7-0000-1000-8000-00805f9b34fb"

# Transparent UART "notify" mode.
BLE_FEC_NOTIFY_UUID = "0000fec8-0000-1000-8000-00805f9b34fb"
BLE_FEC_WRITE_UUID = "0000fec7-0000-1000-8000-00805f9b34fb"

# UART "indicate" mode used by Solar of Things Proximal Monitoring.
BLE_FED_INDICATE_UUID = "0000fed6-0000-1000-8000-00805f9b34fb"
BLE_FED_WRITE_UUID = "0000fed5-0000-1000-8000-00805f9b34fb"

_REQUEST_TIMEOUT = 2.5
_TEMP_POLL_SECONDS = 30.0
_GENERATION_POLL_SECONDS = 60.0

_TRANSPORTS: dict[str, tuple[str, str, bool]] = {
    "fec7_fec8": (BLE_FEC_WRITE_UUID, BLE_FEC_NOTIFY_UUID, False),
    "fed5_fed6": (BLE_FED_WRITE_UUID, BLE_FED_INDICATE_UUID, True),
}


class BleTransportError(RuntimeError):
    """Raised when local BLE telemetry cannot be read."""


def _request_frame(command: str) -> bytes:
    """Frame one read-only HPVINV02 H command."""
    return command.encode("ascii") + b"\r"


def _decode_frame(frame: bytes) -> str:
    """Decode one CR-delimited inverter response frame."""
    if not frame:
        raise BleTransportError("Empty inverter response")

    payload = frame
    if payload.startswith(b"("):
        payload = payload[1:]

    text = payload.decode("ascii", errors="replace").strip()
    if text.startswith("NAK") or text.startswith("NOA"):
        raise BleTransportError(f"Inverter rejected read command: {text}")
    return text


def _tokens(payload: str) -> list[str]:
    return payload.strip().split()


def _float_at(values: list[str], index: int) -> float | None:
    if index >= len(values):
        return None
    try:
        return float(values[index])
    except (TypeError, ValueError):
        return None


def _int_at(values: list[str], index: int) -> int | None:
    value = _float_at(values, index)
    return int(value) if value is not None else None


def _put(raw: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        raw[key] = value


def _parse_hsts(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}
    if values:
        raw["statusCode"] = values[0]
    if len(values) > 1 and values[1]:
        raw["mode"] = values[1][0]
        raw["statusBits"] = values[1][1:]
    if len(values) > 2:
        raw["faultBits"] = values[2]
    return raw


def _parse_hgrid(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}

    _put(raw, "acInputVoltage", _float_at(values, 0))
    _put(raw, "mainsFrequency", _float_at(values, 1))
    _put(raw, "highPointOfMainsPowerLossVoltage", _float_at(values, 2))
    _put(raw, "lowPointOfMainsPowerLossVoltage", _float_at(values, 3))
    _put(raw, "highFrequencyofMainsPowerLoss", _float_at(values, 4))
    _put(raw, "lowFrequencyOfMainsPowerLoss", _float_at(values, 5))

    # HGRID reports watts; the cloud protocol schema exposes mainsPower in kW.
    mains_w = _float_at(values, 6)
    if mains_w is not None:
        raw["mainsPower"] = mains_w / 1000.0

    direction_code = values[7] if len(values) > 7 else None
    if direction_code == "0":
        raw["mainsCurrentFlowDirection"] = "+"
    elif direction_code == "1":
        raw["mainsCurrentFlowDirection"] = "-"

    return raw


def _parse_hop(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}

    _put(raw, "outputVoltage", _float_at(values, 0))
    _put(raw, "outputFrequency", _float_at(values, 1))
    _put(raw, "outputApparentPower", _float_at(values, 2))

    # HOP reports active power in watts; the cloud schema uses kW.
    output_w = _float_at(values, 3)
    if output_w is not None:
        raw["outputActivePower"] = output_w / 1000.0

    _put(raw, "outputLoadPercent", _float_at(values, 4))
    _put(raw, "outputDCComponent", _float_at(values, 5))
    _put(raw, "inductorCurrent", _float_at(values, 7))
    return raw


def _parse_hbat(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}

    _put(raw, "batteryType", _int_at(values, 0))
    _put(raw, "batteryVoltage", _float_at(values, 1))
    _put(raw, "batteryCapacity", _float_at(values, 2))
    _put(raw, "batteryChargingCurrent", _float_at(values, 3))
    _put(raw, "batteryDischargeCurrent", _float_at(values, 4))
    _put(raw, "busVoltage", _float_at(values, 5))
    return raw


def _parse_hpv(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}

    _put(raw, "pvVoltage", _float_at(values, 0))
    _put(raw, "pvCurrent", _float_at(values, 1))

    # HPV reports PV power in watts; the cloud schema exposes pvPower in kW.
    pv_w = _float_at(values, 2)
    if pv_w is not None:
        pv_kw = pv_w / 1000.0
        raw["pvPower"] = pv_kw
        # Generation Power is the portal's instantaneous PV power entity.
        raw["generationPower"] = pv_kw

    return raw


def _parse_htemp(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}
    _put(raw, "inverterTemperature", _float_at(values, 0))
    _put(raw, "boostTemperature", _float_at(values, 1))
    _put(raw, "transformerTemperature", _float_at(values, 2))
    _put(raw, "pvTemperature", _float_at(values, 3))
    _put(raw, "fan1Speed", _float_at(values, 4))
    _put(raw, "fan2Speed", _float_at(values, 5))
    return raw


def _parse_hgen(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    raw: dict[str, Any] = {}
    _put(raw, "pvGeneratedEnergyOfDay", _float_at(values, 2))
    _put(raw, "tqfMonthlyElectricityGeneration", _float_at(values, 3))
    _put(raw, "tqfYearlyElectricityGeneration", _float_at(values, 4))
    _put(raw, "pvGeneratedEnergyOfTotal", _float_at(values, 5))
    return raw


def _parse_qprtl(payload: str) -> dict[str, Any]:
    values = _tokens(payload)
    return {"deviceType": values[0]} if values else {}


_PARSERS = {
    "HSTS": _parse_hsts,
    "HGRID": _parse_hgrid,
    "HOP": _parse_hop,
    "HBAT": _parse_hbat,
    "HPV": _parse_hpv,
    "HTEMP": _parse_htemp,
    "HGEN": _parse_hgen,
    "QPRTL": _parse_qprtl,
}


def _canonical_values(raw: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}

    pv_kw = raw.get("pvPower")
    if isinstance(pv_kw, (int, float)):
        canonical["pvInputPower"] = float(pv_kw) * 1000.0

    output_kw = raw.get("outputActivePower")
    if isinstance(output_kw, (int, float)):
        output_w = float(output_kw) * 1000.0
        canonical["acOutputActivePower"] = output_w
        canonical["loadPower"] = output_w

    voltage = raw.get("batteryVoltage")
    charge = raw.get("batteryChargingCurrent")
    discharge = raw.get("batteryDischargeCurrent")
    soc = raw.get("batteryCapacity")

    if isinstance(voltage, (int, float)):
        canonical["batteryVoltage"] = float(voltage)
    if isinstance(charge, (int, float)):
        canonical["batteryChargingCurrent"] = float(charge)
    if isinstance(discharge, (int, float)):
        canonical["batteryDischargeCurrent"] = float(discharge)
    if isinstance(soc, (int, float)):
        canonical["batterySOC"] = float(soc)

    if isinstance(voltage, (int, float)) and (
        isinstance(charge, (int, float)) or isinstance(discharge, (int, float))
    ):
        canonical["batteryPower"] = (
            float(charge or 0.0) - float(discharge or 0.0)
        ) * float(voltage)

    mains_kw = raw.get("mainsPower")
    direction = raw.get("mainsCurrentFlowDirection")
    if isinstance(mains_kw, (int, float)):
        mains_w = abs(float(mains_kw)) * 1000.0
        if direction == "-" or (direction not in ("+", "-") and float(mains_kw) < 0):
            canonical["gridPower"] = 0.0
            canonical["feedInPower"] = mains_w
        else:
            canonical["gridPower"] = mains_w
            canonical["feedInPower"] = 0.0

    return canonical


class SolarOfThingsBleClient:
    """Local HPVINV02 H-protocol client through a WIFI-RELAB BLE UART."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        self.address = address.strip()
        self._client: BleakClient | None = None
        self._request_lock = asyncio.Lock()
        self._rx_buffer = bytearray()
        self._frames: asyncio.Queue[bytes] = asyncio.Queue()
        self._transport: str | None = None
        self._active_notify_uuid: str | None = None
        self._cached_raw: dict[str, Any] = {}
        self._last_temp_poll = 0.0
        self._last_generation_poll = 0.0
        self._identity_polled = False

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    @property
    def transport(self) -> str | None:
        return self._transport

    def _handle_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        self._active_notify_uuid = None
        self._rx_buffer.clear()

    def _notification_handler(self, _sender: Any, data: bytearray) -> None:
        self._rx_buffer.extend(data)
        while b"\r" in self._rx_buffer:
            frame, _, remainder = self._rx_buffer.partition(b"\r")
            self._rx_buffer = bytearray(remainder)
            if frame:
                self._frames.put_nowait(bytes(frame))

    def _clear_rx(self) -> None:
        self._rx_buffer.clear()
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _connect_for_transport(self, transport: str) -> None:
        if transport not in _TRANSPORTS:
            raise BleTransportError(f"Unknown BLE UART transport: {transport}")

        _, notify_uuid, _ = _TRANSPORTS[transport]
        if self.is_connected and self._active_notify_uuid == notify_uuid:
            return

        await self.async_disconnect()

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise BleTransportError(
                f"BLE collector {self.address} is not currently discovered by Home Assistant"
            )

        client = BleakClient(ble_device, disconnected_callback=self._handle_disconnect)
        try:
            await client.connect()
            await client.start_notify(notify_uuid, self._notification_handler)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise

        self._client = client
        self._active_notify_uuid = notify_uuid
        self._clear_rx()

    async def async_connect(self) -> None:
        """Connect using the detected transport, or the default probe path."""
        await self._connect_for_transport(self._transport or "fec7_fec8")

    async def async_disconnect(self) -> None:
        client = self._client
        notify_uuid = self._active_notify_uuid
        self._client = None
        self._active_notify_uuid = None
        self._clear_rx()

        if client and client.is_connected:
            if notify_uuid:
                try:
                    await client.stop_notify(notify_uuid)
                except Exception:
                    pass
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _request_on_transport(self, command: str, transport: str) -> str:
        await self._connect_for_transport(transport)
        assert self._client is not None

        write_uuid, _, write_with_response = _TRANSPORTS[transport]
        self._clear_rx()

        await self._client.write_gatt_char(
            write_uuid,
            _request_frame(command),
            response=write_with_response,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _REQUEST_TIMEOUT

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BleTransportError(f"Timed out waiting for {command} on {transport}")

            try:
                frame = await asyncio.wait_for(self._frames.get(), timeout=remaining)
            except asyncio.TimeoutError as err:
                raise BleTransportError(
                    f"Timed out waiting for {command} on {transport}"
                ) from err

            text = _decode_frame(frame)
            # Some UART bridges echo our request before forwarding the inverter reply.
            if text == command:
                continue
            return text

    async def _probe_transport(self) -> str:
        errors: list[str] = []
        for transport in ("fec7_fec8", "fed5_fed6"):
            try:
                response = await self._request_on_transport("HSTS", transport)
                if response:
                    self._transport = transport
                    _LOGGER.info(
                        "SolarOfThings BLE %s: HPVINV02 H protocol detected via %s",
                        self.address,
                        transport,
                    )
                    return response
            except Exception as err:
                errors.append(f"{transport}: {err}")
                await self.async_disconnect()

        raise BleTransportError(
            "WIFI-RELAB connected but HPVINV02 HSTS probe failed; " + "; ".join(errors)
        )

    async def async_request(self, command: str) -> str:
        """Send one read-only H command and return the decoded response."""
        if command not in _PARSERS:
            raise BleTransportError(f"Unsupported read-only BLE command: {command}")

        async with self._request_lock:
            if self._transport is None:
                probe_response = await self._probe_transport()
                if command == "HSTS":
                    return probe_response

            assert self._transport is not None
            try:
                return await self._request_on_transport(command, self._transport)
            except Exception:
                # A reconnect or module mode change can invalidate the selected GATT
                # pair. Re-probe once before giving up.
                previous = self._transport
                self._transport = None
                await self.async_disconnect()
                probe_response = await self._probe_transport()
                if command == "HSTS":
                    return probe_response
                _LOGGER.debug(
                    "SolarOfThings BLE %s: transport recovered from %s to %s",
                    self.address,
                    previous,
                    self._transport,
                )
                assert self._transport is not None
                return await self._request_on_transport(command, self._transport)

    async def async_fetch_telemetry(self) -> dict[str, Any]:
        """Read a fresh local inverter snapshot over BLE."""
        raw = dict(self._cached_raw)
        errors: list[str] = []
        successful: list[str] = []

        # Confirmed read-only real-time blocks for POW-HVM6.2KP / HPVINV02.
        for command in ("HSTS", "HGRID", "HOP", "HBAT", "HPV"):
            try:
                response = await self.async_request(command)
                raw.update(_PARSERS[command](response))
                successful.append(command)
            except Exception as err:
                errors.append(f"{command}: {err}")

        loop_time = asyncio.get_running_loop().time()

        if loop_time - self._last_temp_poll >= _TEMP_POLL_SECONDS:
            try:
                response = await self.async_request("HTEMP")
                raw.update(_parse_htemp(response))
                successful.append("HTEMP")
                self._last_temp_poll = loop_time
            except Exception as err:
                errors.append(f"HTEMP: {err}")

        if loop_time - self._last_generation_poll >= _GENERATION_POLL_SECONDS:
            try:
                response = await self.async_request("HGEN")
                raw.update(_parse_hgen(response))
                successful.append("HGEN")
                self._last_generation_poll = loop_time
            except Exception as err:
                errors.append(f"HGEN: {err}")

        if not self._identity_polled:
            try:
                response = await self.async_request("QPRTL")
                raw.update(_parse_qprtl(response))
                successful.append("QPRTL")
                self._identity_polled = True
            except Exception as err:
                errors.append(f"QPRTL: {err}")

        canonical = _canonical_values(raw)

        # HSTS alone proves the serial path but is not enough for live telemetry.
        if not canonical:
            raise BleTransportError(
                "HPVINV02 BLE transport connected but no telemetry decoded"
                + (f" ({'; '.join(errors)})" if errors else "")
            )

        self._cached_raw = raw

        return {
            "raw": raw,
            "canonical": canonical,
            "source": "ble",
            "ble_address": self.address,
            "ble_transport": self._transport,
            "ble_protocol": "HPVINV02 H-command",
            "ble_commands_ok": ",".join(successful),
            "ble_command_errors": "; ".join(errors) if errors else None,
            "cloud_sample_time": None,
            "polled_at": datetime.now(timezone.utc).isoformat(),
        }
