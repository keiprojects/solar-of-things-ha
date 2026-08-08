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
from .controls import write_setting

_LOGGER = logging.getLogger(__name__)

# Confirmed on this inverter's Siseli Control UI.
# Real API key: outputSourcePrioritySetting
# Only two options are exposed by this inverter:
# 0 = SUB, 1 = SBU
OUTPUT_MODE_BY_VALUE: dict[int, str] = {
    0: "Solar First (SUB)",
    1: "Solar+Battery First (SBU)",
}
OUTPUT_MODE_BY_DISPLAY: dict[str, str] = {
    "SUB": "Solar First (SUB)",
    "SBU": "Solar+Battery First (SBU)",
}
OUTPUT_MODES = list(OUTPUT_MODE_BY_VALUE.values())
OUTPUT_MODE_TO_VALUE: dict[str, int] = {v: k for k, v in OUTPUT_MODE_BY_VALUE.items()}

# Confirmed on this inverter's Siseli Control UI.
# Real API key: chargerPrioritySetting
# 0 = CSO, 1 = SNU, 2 = OSO
CHARGER_PRIORITY_BY_VALUE: dict[int, str] = {
    0: "Solar + Utility (CSO)",
    1: "Solar First (SNU)",
    2: "Solar Only (OSO)",
}
CHARGER_PRIORITY_BY_DISPLAY: dict[str, str] = {
    "CSO": "Solar + Utility (CSO)",
    "SNU": "Solar First (SNU)",
    "OSO": "Solar Only (OSO)",
}
CHARGER_PRIORITIES = list(CHARGER_PRIORITY_BY_VALUE.values())
CHARGER_PRIORITY_TO_VALUE: dict[str, int] = {
    v: k for k, v in CHARGER_PRIORITY_BY_VALUE.items()
}


def _setting_details(settings: Any, key: str) -> tuple[Any, str | None]:
    """Return a setting's raw value and Siseli valueDisplay.

    The server-provided valueDisplay is authoritative for enum labels. This
    prevents an inverter-specific numeric value from being shown with the wrong
    Home Assistant label.
    """
    if not isinstance(settings, dict):
        return None, None

    candidates: list[dict[str, Any]] = []

    # Prefer the decoded state objects returned by Siseli because they include
    # both value and valueDisplay, for example value=1, valueDisplay="SBU".
    config_states = settings.get("configAttributeStates")
    if isinstance(config_states, dict):
        candidates.append(config_states)

    # Some cache responses are already a flat key -> setting object mapping.
    candidates.append(settings)

    # targetConfig is a fallback and usually contains only the raw `v` value.
    target_config = settings.get("targetConfig")
    if isinstance(target_config, dict):
        candidates.append(target_config)

    for container in candidates:
        entry = container.get(key)
        if entry is None:
            continue

        if isinstance(entry, dict):
            raw = entry.get("value") if "value" in entry else entry.get("v")
            display = entry.get("valueDisplay")
            return raw, str(display).strip() if display is not None else None

        return entry, None

    return None, None


def _option_from_setting(
    settings: Any,
    key: str,
    by_value: dict[int, str],
    by_display: dict[str, str],
) -> str | None:
    """Resolve the HA option, preferring Siseli's own display label."""
    raw, display = _setting_details(settings, key)

    if display:
        option = by_display.get(display.upper())
        if option is not None:
            return option

    try:
        return by_value.get(int(raw))
    except (TypeError, ValueError):
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
    """Select entity for Output Source Priority (SUB/SBU only)."""

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
        return _option_from_setting(
            settings,
            "outputSourcePrioritySetting",
            OUTPUT_MODE_BY_VALUE,
            OUTPUT_MODE_BY_DISPLAY,
        )

    async def async_select_option(self, option: str) -> None:
        value = OUTPUT_MODE_TO_VALUE.get(option)
        if value is None:
            raise ValueError(f"Unknown output source priority: {option!r}")

        await self.hass.async_add_executor_job(
            write_setting,
            self._api,
            self._device_id,
            "outputSourcePrioritySetting",
            value,
        )
        await self.coordinator.async_request_refresh()


class SolarOfThingsBatteryPrioritySelect(_BaseSelect):
    """Select entity for Charger Priority (CSO/SNU/OSO)."""

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
        return _option_from_setting(
            settings,
            "chargerPrioritySetting",
            CHARGER_PRIORITY_BY_VALUE,
            CHARGER_PRIORITY_BY_DISPLAY,
        )

    async def async_select_option(self, option: str) -> None:
        value = CHARGER_PRIORITY_TO_VALUE.get(option)
        if value is None:
            raise ValueError(f"Unknown charger priority: {option!r}")

        await self.hass.async_add_executor_job(
            write_setting,
            self._api,
            self._device_id,
            "chargerPrioritySetting",
            value,
        )
        await self.coordinator.async_request_refresh()
