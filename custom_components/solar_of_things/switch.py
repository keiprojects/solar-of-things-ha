"""Switch platform for verified Solar of Things inverter controls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .controls import write_setting


@dataclass(frozen=True)
class BinaryControlDescription:
    """Description of one confirmed 0/1 Siseli setting."""

    key: str
    name: str
    icon: str


# These keys and their current 0/1 display meanings were confirmed from this
# inverter's Siseli config readback / Control UI.  They all use 0=off/disable
# and 1=on/enable.
BINARY_CONTROLS: tuple[BinaryControlDescription, ...] = (
    BinaryControlDescription(
        "bmsFunctionEnableSetting",
        "BMS Function Enable",
        "mdi:battery-sync",
    ),
    BinaryControlDescription(
        "backlightOn",
        "Backlight",
        "mdi:lightbulb-on-outline",
    ),
    BinaryControlDescription(
        "buzzerOn",
        "Buzzer",
        "mdi:volume-high",
    ),
    BinaryControlDescription(
        "batteryEqualizationModeEnableSetting",
        "Battery Equalization Mode",
        "mdi:battery-sync-outline",
    ),
    BinaryControlDescription(
        "gridConnectionFunctionEnableSetting",
        "Grid Connection Function",
        "mdi:transmission-tower",
    ),
    BinaryControlDescription(
        "inputSourceDetectionPromptSound",
        "Input Source Detection Sound",
        "mdi:volume-source",
    ),
    BinaryControlDescription(
        "overTemperatureAutomaticRestart",
        "Over Temperature Automatic Restart",
        "mdi:thermometer-alert",
    ),
    BinaryControlDescription(
        "overloadAutomaticRestart",
        "Overload Automatic Restart",
        "mdi:restart-alert",
    ),
    BinaryControlDescription(
        "overloadToBypassOperation",
        "Overload To Bypass",
        "mdi:swap-horizontal-bold",
    ),
    BinaryControlDescription(
        "displayAutomaticallyReturnsToHomepage",
        "Display Auto Return To Homepage",
        "mdi:home-clock-outline",
    ),
    BinaryControlDescription(
        "dualOutputModeEnableSetting",
        "Dual Output Mode",
        "mdi:power-plug-outline",
    ),
)


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


def _setting_entry(settings: Any, key: str) -> Any:
    """Return the setting entry for key from any supported Siseli shape."""
    for container in _settings_containers(settings):
        if key in container:
            return container.get(key)
    return None


def _setting_value(settings: Any, key: str) -> int | None:
    """Return a setting's integer value."""
    entry = _setting_entry(settings, key)
    if entry is None:
        return None

    if isinstance(entry, dict):
        raw = entry.get("value") if "value" in entry else entry.get("v")
    else:
        raw = entry

    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _has_setting(settings: Any, key: str) -> bool:
    """Return True when the inverter actually exposes this setting."""
    return _setting_entry(settings, key) is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    station_id: str = data["station_id"]
    device_coordinators = data["device_coordinators"]

    entities: list[SwitchEntity] = []

    for device_id, coordinator in device_coordinators.items():
        device_name = (coordinator.device_meta or {}).get("name") or device_id
        settings = (coordinator.data or {}).get("settings") or {}

        for description in BINARY_CONTROLS:
            # Do not create controls for settings this inverter does not expose.
            if not _has_setting(settings, description.key):
                continue

            entities.append(
                SolarOfThingsBinarySettingSwitch(
                    api,
                    coordinator,
                    station_id,
                    device_id,
                    device_name,
                    description,
                )
            )

    async_add_entities(entities)


class SolarOfThingsBinarySettingSwitch(CoordinatorEntity, SwitchEntity):
    """A verified Siseli 0/1 setting exposed as a Home Assistant switch."""

    def __init__(
        self,
        api,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
        description: BinaryControlDescription,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._station_id = station_id
        self._device_id = device_id
        self._device_name = device_name
        self._description = description

        self._attr_name = f"{device_name} {description.name}"
        self._attr_unique_id = (
            f"{DOMAIN}_{station_id}_{device_id}_control_{description.key}"
        )
        self._attr_icon = description.icon

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Siseli",
            "model": (self.coordinator.device_meta or {}).get("model"),
            "via_device": (DOMAIN, self._station_id),
        }

    @property
    def is_on(self) -> bool | None:
        settings = (self.coordinator.data or {}).get("settings") or {}
        value = _setting_value(settings, self._description.key)
        if value is None:
            return None
        return value == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(
            write_setting,
            self._api,
            self._device_id,
            self._description.key,
            1,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(
            write_setting,
            self._api,
            self._device_id,
            self._description.key,
            0,
        )
        await self.coordinator.async_request_refresh()
