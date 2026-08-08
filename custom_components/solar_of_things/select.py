"""Select platform for Solar of Things integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Confirmed against the Siseli portal control UI / live device readback.
# Real API key: outputSourcePrioritySetting
# 0 = SUB, 1 = SBU, 2 = USO
OUTPUT_MODE_BY_VALUE: dict[int, str] = {
    0: "Solar First (SUB)",
    1: "Solar+Battery First (SBU)",
    2: "Utility First (USO)",
}
OUTPUT_MODES = list(OUTPUT_MODE_BY_VALUE.values())

# Confirmed against the Siseli portal control UI / live device readback.
# Real API key: chargerPrioritySetting
# 0 = CSO, 1 = SNU, 2 = OSO
CHARGER_PRIORITY_BY_VALUE: dict[int, str] = {
    0: "Solar + Utility (CSO)",
    1: "Solar First (SNU)",
    2: "Solar Only (OSO)",
}
CHARGER_PRIORITIES = list(CHARGER_PRIORITY_BY_VALUE.values())


def _setting_value(settings: Any, key: str) -> Any:
    """Return a setting value from the Siseli cached/batch-read response shapes."""
    if not isinstance(settings, dict):
        return None

    candidates = [settings]
    for container_key in ("configAttributeStates", "targetConfig"):
        nested = settings.get(container_key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for container in candidates:
        entry = container.get(key)
        if entry is None:
            continue
        if isinstance(entry, dict):
            if "value" in entry:
                return entry.get("value")
            if "v" in entry:
                return entry.get("v")
        return entry

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    station_id: str = data["station_id"]
    device_coordinators = data["device_coordinators"]

    entities: list[SelectEntity] = []

    for device_id, coordinator in device_coordinators.items():
        device_name = (coordinator.device_meta or {}).get("name") or device_id
        entities.extend(
            [
                SolarOfThingsOperatingModeSelect(
                    api, coordinator, station_id, device_id, device_name
                ),
                SolarOfThingsBatteryPrioritySelect(
                    api, coordinator, station_id, device_id, device_name
                ),
            ]
        )

    async_add_entities(entities)


class _BaseSelect(CoordinatorEntity, SelectEntity):
    def __init__(
        self,
        api,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._station_id = station_id
        self._device_id = device_id
        self._device_name = device_name

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Siseli",
            "model": (
                (self.coordinator.data.get("device_meta") or {}).get("model")
                if self.coordinator.data
                else None
            ),
            "via_device": (DOMAIN, self._station_id),
        }


class SolarOfThingsOperatingModeSelect(_BaseSelect):
    """Select entity for Output Source Priority (outputSourcePrioritySetting)."""

    def __init__(
        self,
        api,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(api, coordinator, station_id, device_id, device_name)
        self._attr_name = f"{device_name} Output Source Priority"
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_operating_mode"
        self._attr_options = OUTPUT_MODES
        self._attr_icon = "mdi:cog"

    @property
    def current_option(self) -> str | None:
        settings = (self.coordinator.data or {}).get("settings") or {}
        raw = _setting_value(settings, "outputSourcePrioritySetting")
        try:
            return OUTPUT_MODE_BY_VALUE.get(int(raw))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        await self.hass.async_add_executor_job(
            self._api.set_operating_mode, self._device_id, option
        )
        await self.coordinator.async_request_refresh()


class SolarOfThingsBatteryPrioritySelect(_BaseSelect):
    """Select entity for Charger Priority (chargerPrioritySetting)."""

    def __init__(
        self,
        api,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(api, coordinator, station_id, device_id, device_name)
        self._attr_name = f"{device_name} Charger Priority"
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_battery_priority"
        self._attr_options = CHARGER_PRIORITIES
        self._attr_icon = "mdi:battery-sync"

    @property
    def current_option(self) -> str | None:
        settings = (self.coordinator.data or {}).get("settings") or {}
        raw = _setting_value(settings, "chargerPrioritySetting")
        try:
            return CHARGER_PRIORITY_BY_VALUE.get(int(raw))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        await self.hass.async_add_executor_job(
            self._api.set_battery_priority, self._device_id, option
        )
        await self.coordinator.async_request_refresh()
