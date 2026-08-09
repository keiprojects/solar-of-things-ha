"""The Solar of Things integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SolarOfThingsAPI, TokenExpiredError
from .ble import SolarOfThingsBleClient
from .const import (
    DOMAIN,
    CONF_USER_ID,
    CONF_PASSWORD,
    CONF_IOT_TOKEN,
    CONF_STATION_ID,
    CONF_DEVICE_ID,
    CONF_TIME_ZONE,
    CONF_BLE_ADDRESS,
    CONF_REFRESH_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES,
    CONF_REFRESH_TOKEN_EXPIRES,
)
from .telemetry import fetch_latest_telemetry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

DEVICE_UPDATE_INTERVAL = timedelta(seconds=10)
BLE_DEVICE_UPDATE_INTERVAL = timedelta(seconds=5)
STATION_UPDATE_INTERVAL = timedelta(minutes=30)
BLE_SETTINGS_UPDATE_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar of Things from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    time_zone = entry.data.get(CONF_TIME_ZONE) or entry.options.get(CONF_TIME_ZONE)
    user_id = entry.data.get(CONF_USER_ID)
    password = entry.data.get(CONF_PASSWORD)

    def _on_token_refreshed(
        access_token: str,
        refresh_token: str,
        access_expires: str,
        refresh_expires: str,
    ) -> None:
        @callback
        def _update() -> None:
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_IOT_TOKEN: access_token,
                    CONF_REFRESH_TOKEN: refresh_token,
                    CONF_ACCESS_TOKEN_EXPIRES: access_expires,
                    CONF_REFRESH_TOKEN_EXPIRES: refresh_expires,
                },
            )

        hass.loop.call_soon_threadsafe(_update)

    if user_id and password:
        api = SolarOfThingsAPI(
            user_id=user_id,
            password=password,
            iot_token=entry.data.get(CONF_IOT_TOKEN),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            access_token_expires=entry.data.get(CONF_ACCESS_TOKEN_EXPIRES),
            refresh_token_expires=entry.data.get(CONF_REFRESH_TOKEN_EXPIRES),
            time_zone=time_zone,
            on_token_refreshed=_on_token_refreshed,
        )
        if not api.access_token:
            await hass.async_add_executor_job(api.login)
    else:
        api = SolarOfThingsAPI(
            iot_token=entry.data[CONF_IOT_TOKEN],
            time_zone=time_zone,
            on_token_refreshed=_on_token_refreshed,
        )

    station_id = entry.data[CONF_STATION_ID]
    configured_device_id = (entry.data.get(CONF_DEVICE_ID) or "").strip()
    ble_address = (
        entry.options.get(CONF_BLE_ADDRESS)
        or entry.data.get(CONF_BLE_ADDRESS)
        or ""
    ).strip()

    station_coordinator = SolarOfThingsStationCoordinator(
        hass=hass,
        api=api,
        station_id=station_id,
        entry=entry,
    )
    await station_coordinator.async_config_entry_first_refresh()

    devices: list[dict[str, Any]] = (
        station_coordinator.data.get("devices", []) if station_coordinator.data else []
    )

    if configured_device_id:
        filtered = [d for d in devices if str(d.get("id")) == configured_device_id]
        devices = filtered if filtered else [
            {"id": configured_device_id, "name": configured_device_id}
        ]

    ble_client: SolarOfThingsBleClient | None = None
    ble_target_device_id: str | None = None
    if ble_address:
        ble_client = SolarOfThingsBleClient(hass, ble_address)
        if configured_device_id:
            ble_target_device_id = configured_device_id
        elif devices:
            ble_target_device_id = str(devices[0].get("id") or "") or None
        if len(devices) > 1 and not configured_device_id:
            _LOGGER.warning(
                "SolarOfThings: BLE address %s will be attached to the first inverter %s. "
                "Set Device ID in the integration if this is not the intended inverter.",
                ble_address,
                ble_target_device_id,
            )

    device_coordinators: dict[str, SolarOfThingsDeviceCoordinator] = {}
    for dev in devices:
        device_id = str(dev.get("id") or "")
        if not device_id:
            continue
        coordinator = SolarOfThingsDeviceCoordinator(
            hass=hass,
            api=api,
            station_id=station_id,
            device=device_id,
            device_meta=dev,
            entry=entry,
            ble_client=ble_client if device_id == ble_target_device_id else None,
        )
        await coordinator.async_config_entry_first_refresh()
        device_coordinators[device_id] = coordinator

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "station_id": station_id,
        "station_coordinator": station_coordinator,
        "device_coordinators": device_coordinators,
        "devices": devices,
        "ble_client": ble_client,
        "ble_target_device_id": ble_target_device_id,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, station_id)},
        name=f"Solar Station {station_id}",
        manufacturer="Siseli",
        model="Station",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        ble_client = runtime.get("ble_client")
        if ble_client:
            try:
                await ble_client.async_disconnect()
            except Exception as err:
                _LOGGER.debug("BLE disconnect failed during unload: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


class SolarOfThingsStationCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarOfThingsAPI,
        station_id: str,
        entry: ConfigEntry,
    ) -> None:
        self.api = api
        self.station_id = station_id
        self._entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_station_{station_id}",
            update_interval=STATION_UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            devices = await self.hass.async_add_executor_job(
                self.api.list_devices, self.station_id
            )
            monthly = await self.hass.async_add_executor_job(
                self.api.fetch_monthly_summary, self.station_id
            )
            return {"devices": devices, "monthly": monthly}
        except TokenExpiredError as err:
            self._entry.async_start_reauth(self.hass)
            raise UpdateFailed(f"Token expired: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Station update failed: {err}") from err


class SolarOfThingsDeviceCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        api: SolarOfThingsAPI,
        station_id: str,
        device: str,
        device_meta: dict[str, Any],
        entry: ConfigEntry,
        ble_client: SolarOfThingsBleClient | None = None,
    ) -> None:
        self.api = api
        self.station_id = station_id
        self.device_id = device
        self.device_meta = device_meta
        self._entry = entry
        self.ble_client = ble_client
        self._settings_cache: dict[str, Any] = {}
        self._settings_last_refresh: datetime | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_device_{device}",
            update_interval=(
                BLE_DEVICE_UPDATE_INTERVAL if ble_client else DEVICE_UPDATE_INTERVAL
            ),
        )

    async def _async_fetch_time_series(self) -> dict[str, Any]:
        """Prefer local BLE and fall back to the existing cloud telemetry path."""
        if self.ble_client is not None:
            try:
                return await self.ble_client.async_fetch_telemetry()
            except Exception as err:
                _LOGGER.warning(
                    "SolarOfThings device %s: local BLE telemetry failed (%s); "
                    "falling back to cloud",
                    self.device_id,
                    err,
                )
                cloud = await self.hass.async_add_executor_job(
                    fetch_latest_telemetry, self.api, self.device_id
                )
                cloud["ble_error"] = str(err)
                cloud["ble_address"] = self.ble_client.address
                return cloud

        return await self.hass.async_add_executor_job(
            fetch_latest_telemetry, self.api, self.device_id
        )

    async def _async_fetch_settings(self) -> dict[str, Any]:
        """Keep cloud settings available without slowing the 5-second BLE loop."""
        now = datetime.now(timezone.utc)
        if (
            self.ble_client is not None
            and self._settings_last_refresh is not None
            and now - self._settings_last_refresh < BLE_SETTINGS_UPDATE_INTERVAL
        ):
            return self._settings_cache

        try:
            settings = await self.hass.async_add_executor_job(
                self.api.fetch_settings, self.device_id
            )
        except TokenExpiredError:
            if self.ble_client is None:
                raise
            _LOGGER.warning(
                "SolarOfThings device %s: cloud token expired while BLE telemetry "
                "is active; keeping cached settings",
                self.device_id,
            )
            return self._settings_cache
        except Exception as err:
            _LOGGER.warning(
                "SolarOfThings device %s: parameter read unavailable: %s",
                self.device_id,
                err,
            )
            return self._settings_cache

        if isinstance(settings, dict):
            self._settings_cache = settings
        self._settings_last_refresh = now
        return self._settings_cache

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            time_series = await self._async_fetch_time_series()
            settings = await self._async_fetch_settings()

            return {
                "time_series": time_series,
                "settings": settings,
                "device": self.device_id,
                "station_id": self.station_id,
                "device_meta": self.device_meta,
            }
        except TokenExpiredError as err:
            self._entry.async_start_reauth(self.hass)
            raise UpdateFailed(f"Token expired: {err}") from err
        except Exception as err:
            raise UpdateFailed(
                f"Device update failed for {self.device_id}: {err}"
            ) from err
