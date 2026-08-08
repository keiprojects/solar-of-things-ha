"""Select platform for verified Solar of Things inverter controls."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .controls import write_setting


# Confirmed on this inverter's Siseli Control UI.
# Real API key: outputSourcePrioritySetting
# This inverter exposes only SUB and SBU.
OUTPUT_MODE_BY_VALUE: dict[int, str] = {
    0: "Solar First (SUB)",
    1: "Solar+Battery First (SBU)",
}
OUTPUT_MODE_BY_DISPLAY: dict[str, str] = {
    "SUB": "Solar First (SUB)",
    "SBU": "Solar+Battery First (SBU)",
}
OUTPUT_MODES = list(OUTPUT_MODE_BY_VALUE.values())
OUTPUT_MODE_TO_VALUE: dict[str, int] = {
    option: value for value, option in OUTPUT_MODE_BY_VALUE.items()
}


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
    option: value for value, option in CHARGER_PRIORITY_BY_VALUE.items()
}


# POW-HVM6.2KP program 11 is not a regular number sequence:
# it allows 2A, then 10A..100A in 10A increments. A SelectEntity prevents
# Home Assistant from offering invalid values such as 12A or 22A.
MAX_MAINS_CHARGE_VALUES = [2, *range(10, 101, 10)]
MAX_MAINS_CHARGE_BY_VALUE: dict[int, str] = {
    value: f"{value} A" for value in MAX_MAINS_CHARGE_VALUES
}
MAX_MAINS_CHARGE_OPTIONS = list(MAX_MAINS_CHARGE_BY_VALUE.values())
MAX_MAINS_CHARGE_TO_VALUE: dict[str, int] = {
    option: value for value, option in MAX_MAINS_CHARGE_BY_VALUE.items()
}


def _settings_containers(settings: Any) -> list[dict[str, Any]]:
    """Return possible setting containers in preferred order."""
    if not isinstance(settings, dict):
        return []

    containers: list[dict[str, Any]] = []

    config_states = settings.get("configAttributeStates")
    if isinstance(config_states, dict):
        containers.append(config_states)

    containers.append(settings)

    target_config = settings.get("targetConfig")
    if isinstance(target_config, dict):
        containers.append(target_config)

    return containers


def _setting_details(settings: Any, key: str) -> tuple[Any, str | None]:
    """Return a setting's raw value and Siseli valueDisplay.

    The server-provided valueDisplay is authoritative for enum labels. This
    prevents an inverter-specific numeric value from being shown with the wrong
    Home Assistant label.
    """
    for container in _settings_containers(settings):
        entry = container.get(key)
        if entry is None:
            continue

        if isinstance(entry, dict):
            raw = entry.get("value") if "value" in entry else entry.get("v")
            display = entry.get("valueDisplay")
            return raw, str(display).strip() if display is not None else None

        return entry, None

    return None, None


def _has_setting(settings: Any, key: str) -> bool:
    """Return True when the inverter exposes this setting."""
    for container in _settings_containers(settings):
        if key in container:
            return True
    return False


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
        settings = (coordinator.data or {}).get("settings") or {}

        if _has_setting(settings, "outputSourcePrioritySetting"):
            entities.append(
                SolarOfThingsOperatingModeSelect(
                    api, coordinator, station_id, device_id, device_name
                )
            )

        if _has_setting(settings, "chargerPrioritySetting"):
            entities.append(
                SolarOfThingsChargerPrioritySelect(
                    api, coordinator, station_id, device_id, device_name
                )
            )

        if _has_setting(settings, "maximumMainsChargingCurrentSetting"):
            entities.append(
                SolarOfThingsMaximumMainsChargingCurrentSelect(
                    api, coordinator, station_id, device_id, device_name
                )
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
            "model": (self.coordinator.device_meta or {}).get("model"),
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


class SolarOfThingsChargerPrioritySelect(_BaseSelect):
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
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_charger_priority"
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


class SolarOfThingsMaximumMainsChargingCurrentSelect(_BaseSelect):
    """Select entity for the inverter's non-linear utility-charge current list."""

    def __init__(
        self,
        api,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
    ) -> None:
        super().__init__(api, coordinator, station_id, device_id, device_name)
        self._attr_name = f"{device_name} Maximum Utility Charging Current"
        self._attr_unique_id = (
            f"{DOMAIN}_{station_id}_{device_id}_maximum_mains_charging_current"
        )
        self._attr_options = MAX_MAINS_CHARGE_OPTIONS
        self._attr_icon = "mdi:transmission-tower-import"

    @property
    def current_option(self) -> str | None:
        settings = (self.coordinator.data or {}).get("settings") or {}
        raw, _display = _setting_details(
            settings, "maximumMainsChargingCurrentSetting"
        )
        try:
            return MAX_MAINS_CHARGE_BY_VALUE.get(int(raw))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        value = MAX_MAINS_CHARGE_TO_VALUE.get(option)
        if value is None:
            raise ValueError(
                f"Unknown maximum utility charging current: {option!r}"
            )

        await self.hass.async_add_executor_job(
            write_setting,
            self._api,
            self._device_id,
            "maximumMainsChargingCurrentSetting",
            value,
        )
        await self.coordinator.async_request_refresh()
