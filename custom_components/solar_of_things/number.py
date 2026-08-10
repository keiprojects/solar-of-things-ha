"""Number platform for verified Solar of Things inverter controls."""
from __future__ import annotations

from dataclasses import dataclass
from math import isclose
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .controls import write_setting


@dataclass(frozen=True)
class NumericControlDescription:
    """Description of one verified numeric Siseli inverter setting."""

    key: str
    name: str
    icon: str
    minimum: float
    maximum: float
    step: float
    unit: str
    allowed_values: tuple[float, ...] | None = None


# POW-HVM6.2KP manual-backed numeric settings.
#
# Program 02: 10A..120A, 10A steps
# Program 11: 2A, then 10A..100A in 10A increments
# Program 12: 44V..51V, 1V steps
# Program 13: 48V..58V, 1V steps
# Program 24: 40V..54V (the LCD uses 0.1V precision)
# Program 26: 48V..60V, 0.1V steps
# Program 27: 48V..54V, 0.1V steps
# Program 29: 40V..52V, 0.1V steps
# Program 31: 48V..60V, 0.1V steps
# Program 33/34: 5..900 min, 5 min steps
# Program 35: 0..90 days, 1 day steps
MAX_MAINS_CHARGE_VALUES: tuple[float, ...] = (
    2,
    10,
    20,
    30,
    40,
    50,
    60,
    70,
    80,
    90,
    100,
)

NUMERIC_CONTROLS: tuple[NumericControlDescription, ...] = (
    NumericControlDescription(
        "maximumChargingCurrentSetting",
        "Maximum Charging Current",
        "mdi:current-dc",
        10,
        120,
        10,
        "A",
    ),
    NumericControlDescription(
        "maximumMainsChargingCurrentSetting",
        "Maximum Utility Charging Current",
        "mdi:transmission-tower-import",
        2,
        100,
        1,
        "A",
        MAX_MAINS_CHARGE_VALUES,
    ),
    NumericControlDescription(
        "batteryRechargeVoltageSetting",
        "Back To Utility Voltage",
        "mdi:transmission-tower-import",
        44,
        51,
        1,
        "V",
    ),
    NumericControlDescription(
        "batteryRedischargeVoltageSetting",
        "Back To Battery Voltage",
        "mdi:battery-arrow-up",
        48,
        58,
        1,
        "V",
    ),
    NumericControlDescription(
        "lowBatteryAlarmVoltageSetting",
        "Low Battery Alarm Voltage",
        "mdi:battery-alert-variant-outline",
        40,
        54,
        0.1,
        "V",
    ),
    NumericControlDescription(
        "batteryConstantChargingVoltageSetting",
        "Bulk Charging Voltage",
        "mdi:battery-charging-high",
        48,
        60,
        0.1,
        "V",
    ),
    NumericControlDescription(
        "batteryFloatChargingVoltageSetting",
        "Float Charging Voltage",
        "mdi:battery-charging-medium",
        48,
        54,
        0.1,
        "V",
    ),
    NumericControlDescription(
        "batteryCutOffVoltageSetting",
        "Low DC Cut-off Voltage",
        "mdi:battery-off-outline",
        40,
        52,
        0.1,
        "V",
    ),
    NumericControlDescription(
        "batteryEqualizationVoltageSetting",
        "Battery Equalization Voltage",
        "mdi:battery-sync-outline",
        48,
        60,
        0.1,
        "V",
    ),
    NumericControlDescription(
        "batteryEqualizationTimeSetting",
        "Battery Equalization Time",
        "mdi:timer-outline",
        5,
        900,
        5,
        "min",
    ),
    NumericControlDescription(
        "batteryEqualizationTimeoutSetting",
        "Battery Equalization Timeout",
        "mdi:timer-alert-outline",
        5,
        900,
        5,
        "min",
    ),
    NumericControlDescription(
        "batteryEqualizationIntervalSetting",
        "Battery Equalization Interval",
        "mdi:calendar-sync-outline",
        0,
        90,
        1,
        "day",
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
    """Return the raw setting entry for key from any supported Siseli shape."""
    for container in _settings_containers(settings):
        if key in container:
            return container.get(key)
    return None


def _setting_value(settings: Any, key: str) -> float | None:
    """Return the current numeric value for a setting."""
    entry = _setting_entry(settings, key)
    if entry is None:
        return None

    if isinstance(entry, dict):
        raw = entry.get("value") if "value" in entry else entry.get("v")
    else:
        raw = entry

    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _has_setting(settings: Any, key: str) -> bool:
    """Return True when this inverter exposes the setting."""
    return _setting_entry(settings, key) is not None


def _decimal_places(step: float) -> int:
    """Return a suitable decimal precision from the configured step."""
    text = f"{step:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.rsplit(".", 1)[1])


def _remove_legacy_numeric_select(
    hass: HomeAssistant,
    entry: ConfigEntry,
    station_id: str,
    device_id: str,
) -> None:
    """Remove the old utility-charge-current select after converting to number."""
    registry = er.async_get(hass)
    legacy_unique_id = (
        f"{DOMAIN}_{station_id}_{device_id}_maximum_mains_charging_current"
    )
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registry_entry.domain == "select"
            and registry_entry.unique_id == legacy_unique_id
        ):
            registry.async_remove(registry_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up verified numeric inverter controls."""
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    station_id: str = data["station_id"]
    device_coordinators = data["device_coordinators"]

    entities: list[NumberEntity] = []

    for device_id, coordinator in device_coordinators.items():
        device_name = (coordinator.device_meta or {}).get("name") or device_id
        settings = (coordinator.data or {}).get("settings") or {}

        _remove_legacy_numeric_select(hass, entry, station_id, device_id)

        for description in NUMERIC_CONTROLS:
            if not _has_setting(settings, description.key):
                continue

            entities.append(
                SolarOfThingsNumericSettingNumber(
                    api,
                    coordinator,
                    station_id,
                    device_id,
                    device_name,
                    description,
                )
            )

    async_add_entities(entities)


class SolarOfThingsNumericSettingNumber(CoordinatorEntity, NumberEntity):
    """A verified writable numeric Siseli inverter setting."""

    def __init__(
        self,
        api,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
        description: NumericControlDescription,
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
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum
        self._attr_native_step = description.step
        self._attr_native_unit_of_measurement = description.unit
        self._attr_mode = NumberMode.BOX

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
    def native_value(self) -> float | None:
        settings = (self.coordinator.data or {}).get("settings") or {}
        value = _setting_value(settings, self._description.key)
        if value is None:
            return None

        precision = _decimal_places(self._description.step)
        return round(value, precision)

    async def async_set_native_value(self, value: float) -> None:
        requested = float(value)
        minimum = self._description.minimum
        maximum = self._description.maximum
        step = self._description.step

        if requested < minimum or requested > maximum:
            raise ValueError(
                f"{self._description.name} must be between "
                f"{minimum:g} and {maximum:g} {self._description.unit}"
            )

        allowed_values = self._description.allowed_values
        if allowed_values is not None and not any(
            isclose(requested, allowed, abs_tol=1e-7) for allowed in allowed_values
        ):
            allowed_text = ", ".join(f"{item:g}" for item in allowed_values)
            raise ValueError(
                f"{self._description.name} must be one of: "
                f"{allowed_text} {self._description.unit}"
            )

        steps = (requested - minimum) / step
        if not isclose(steps, round(steps), abs_tol=1e-7):
            raise ValueError(
                f"{self._description.name} must use {step:g} "
                f"{self._description.unit} increments"
            )

        precision = _decimal_places(step)
        outgoing: int | float
        rounded = round(requested, precision)
        outgoing = int(rounded) if precision == 0 else rounded

        await self.hass.async_add_executor_job(
            write_setting,
            self._api,
            self._device_id,
            self._description.key,
            outgoing,
        )
        await self.coordinator.async_request_refresh()
