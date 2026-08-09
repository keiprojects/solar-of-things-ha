"""Local Bluetooth transport for Solar of Things / WiFi Relabs collectors.

The collector exposes the inverter's RS232 stream over a BLE UART-style GATT
service. This module speaks the common Voltronic/PI serial protocol through that
bridge and normalises the live QPIGS response into the same canonical keys used
by the cloud telemetry path.
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

# High-Flying BLE UART defaults used by RWB1-class Wi-Fi/BLE serial collectors.
BLE_SERVICE_UUID = "0000fee7-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_UUID = "0000fec8-0000-1000-8000-00805f9b34fb"
BLE_WRITE_UUID = "0000fec7-0000-1000-8000-00805f9b34fb"

_REQUEST_TIMEOUT = 4.0


class BleTransportError(RuntimeError):
    """Raised when local BLE telemetry cannot be read."""


def _crc16_xmodem(data: bytes) -> int:
    """Return the PI/Voltronic CRC16-XMODEM value, including byte escaping."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    high = (crc >> 8) & 0xFF
    low = crc & 0xFF
    if high in (0x28, 0x0D, 0x0A):
        high = (high + 1) & 0xFF
    if low in (0x28, 0x0D, 0x0A):
        low = (low + 1) & 0xFF
    return (high << 8) | low


def _frame_command(command: str) -> bytes:
    payload = command.encode("ascii")
    crc = _crc16_xmodem(payload)
    return payload + bytes(((crc >> 8) & 0xFF, crc & 0xFF, 0x0D))


def _decode_frame(frame: bytes) -> str:
    """Validate and decode one response frame without the trailing CR."""
    if len(frame) < 3:
        raise BleTransportError(f"Short inverter response: {frame!r}")

    payload = frame[:-2]
    received_crc = (frame[-2] << 8) | frame[-1]
    expected_crc = _crc16_xmodem(payload)
    if received_crc != expected_crc:
        raise BleTransportError(
            f"Bad inverter CRC: received=0x{received_crc:04X} "
            f"expected=0x{expected_crc:04X}"
        )

    if payload.startswith(b"("):
        payload = payload[1:]

    text = payload.decode("ascii", errors="replace").strip()
    if text in ("NAK", "NOA"):
        raise BleTransportError(f"Inverter rejected command: {text}")
    return text


def _number(fields: list[str], index: int) -> float | None:
    if index >= len(fields):
        return None
    try:
        return float(fields[index])
    except (TypeError, ValueError):
        return None


def _parse_qpigs(payload: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Parse the common 17/21/24-field QPIGS layouts."""
    fields = payload.split()
    field_count = len(fields)
    if field_count < 17:
        raise BleTransportError(
            f"Unsupported QPIGS response with {field_count} fields: {payload}"
        )

    raw: dict[str, Any] = {
        "mainsVoltage": _number(fields, 0),
        "mainsFrequency": _number(fields, 1),
        "outputVoltage": _number(fields, 2),
        "outputFrequency": _number(fields, 3),
        "outputApparentPower": _number(fields, 4),
        "outputActivePower": _number(fields, 5),
        "outputLoadPercent": _number(fields, 6),
        "busVoltage": _number(fields, 7),
        "batteryVoltage": _number(fields, 8),
        "batteryChargingCurrent": _number(fields, 9),
        "batteryCapacity": _number(fields, 10),
        "inverterTemperature": _number(fields, 11),
        "pvInputCurrent": _number(fields, 12),
        "pvInputVoltage": _number(fields, 13),
        "batterySccVoltage": _number(fields, 14),
        "batteryDischargeCurrent": _number(fields, 15),
        "deviceStatus": fields[16],
    }

    if field_count >= 21:
        raw.update(
            {
                "batteryVoltageOffsetForFans": fields[17],
                "eepromVersion": fields[18],
                "pvChargingPower": _number(fields, 19),
                "extendedDeviceStatus": fields[20],
            }
        )
    if field_count >= 24:
        raw.update(
            {
                "solarFeedToGridStatus": fields[21],
                "countryCode": fields[22],
                "feedInPower": _number(fields, 23),
            }
        )

    pv_power = raw.get("pvChargingPower")
    if pv_power is None:
        pv_voltage = raw.get("pvInputVoltage")
        pv_current = raw.get("pvInputCurrent")
        if pv_voltage is not None and pv_current is not None:
            pv_power = pv_voltage * pv_current

    battery_voltage = raw.get("batteryVoltage")
    charge_current = raw.get("batteryChargingCurrent")
    discharge_current = raw.get("batteryDischargeCurrent")

    canonical: dict[str, Any] = {}
    if pv_power is not None:
        canonical["pvInputPower"] = pv_power
    if raw.get("outputActivePower") is not None:
        canonical["acOutputActivePower"] = raw["outputActivePower"]
        canonical["loadPower"] = raw["outputActivePower"]
    if battery_voltage is not None:
        canonical["batteryVoltage"] = battery_voltage
    if charge_current is not None:
        canonical["batteryChargingCurrent"] = charge_current
    if discharge_current is not None:
        canonical["batteryDischargeCurrent"] = discharge_current
    if raw.get("batteryCapacity") is not None:
        canonical["batterySOC"] = raw["batteryCapacity"]
    if battery_voltage is not None and (
        charge_current is not None or discharge_current is not None
    ):
        canonical["batteryPower"] = (
            (charge_current or 0.0) - (discharge_current or 0.0)
        ) * battery_voltage
    if raw.get("feedInPower") is not None:
        canonical["feedInPower"] = raw["feedInPower"]

    return raw, canonical, field_count


class SolarOfThingsBleClient:
    """BLE UART client for a WiFi Relabs / Solar of Things collector."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        self.address = address.strip()
        self._client: BleakClient | None = None
        self._request_lock = asyncio.Lock()
        self._rx_buffer = bytearray()
        self._frames: asyncio.Queue[bytes] = asyncio.Queue()
        self._protocol_id: str | None = None

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def _handle_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        self._rx_buffer.clear()

    def _notification_handler(self, _sender: Any, data: bytearray) -> None:
        self._rx_buffer.extend(data)
        while b"\r" in self._rx_buffer:
            frame, _, remainder = self._rx_buffer.partition(b"\r")
            self._rx_buffer = bytearray(remainder)
            if frame:
                self._frames.put_nowait(bytes(frame))

    async def async_connect(self) -> None:
        if self.is_connected:
            return

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
            await client.start_notify(BLE_NOTIFY_UUID, self._notification_handler)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self._client = client

    async def async_disconnect(self) -> None:
        client = self._client
        self._client = None
        self._rx_buffer.clear()
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break
        if client and client.is_connected:
            try:
                await client.stop_notify(BLE_NOTIFY_UUID)
            except Exception:
                pass
            await client.disconnect()

    async def async_request(self, command: str) -> str:
        """Send one PI command and return its decoded payload."""
        async with self._request_lock:
            await self.async_connect()
            assert self._client is not None

            while not self._frames.empty():
                try:
                    self._frames.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._rx_buffer.clear()

            try:
                await self._client.write_gatt_char(
                    BLE_WRITE_UUID, _frame_command(command), response=False
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _REQUEST_TIMEOUT
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    frame = await asyncio.wait_for(self._frames.get(), timeout=remaining)
                    text = _decode_frame(frame)
                    # Some UART bridges echo the command before forwarding the
                    # inverter response. Ignore that echo and wait for the next frame.
                    if text == command:
                        continue
                    return text
            except asyncio.TimeoutError as err:
                await self.async_disconnect()
                raise BleTransportError(
                    f"Timed out waiting for {command} from BLE collector {self.address}"
                ) from err
            except Exception:
                await self.async_disconnect()
                raise

    async def async_fetch_telemetry(self) -> dict[str, Any]:
        """Read a fresh local inverter snapshot over BLE."""
        if self._protocol_id is None:
            try:
                self._protocol_id = await self.async_request("QPI")
            except Exception as err:
                _LOGGER.debug("BLE QPI probe failed; continuing with QPIGS: %s", err)

        qpigs = await self.async_request("QPIGS")
        raw, canonical, field_count = _parse_qpigs(qpigs)

        mode: str | None = None
        try:
            mode = await self.async_request("QMOD")
            if mode:
                raw["inverterOperationMode"] = mode
        except Exception as err:
            _LOGGER.debug("BLE QMOD read unavailable: %s", err)

        return {
            "raw": raw,
            "canonical": canonical,
            "source": "ble",
            "ble_address": self.address,
            "protocol_id": self._protocol_id,
            "mode": mode,
            "qpigs_field_count": field_count,
            "raw_qpigs": qpigs,
            "cloud_sample_time": None,
            "polled_at": datetime.now(timezone.utc).isoformat(),
        }
