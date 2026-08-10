"""Sensor platform for the inverter-specific Solar of Things fork."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSOR_DEFINITIONS
from .telemetry import PROTOCOL_SCHEMA

_TRANSLATION_KEYS: dict[str, str] = {
    "pvInputPower": "pv_input_power",
    "batteryDischargeCurrent": "battery_discharge_current",
    "batteryChargingCurrent": "battery_charging_current",
    "batteryVoltage": "battery_voltage",
    "batteryPower": "battery_power",
    "batterySOC": "battery_soc",
    "gridPower": "grid_power",
    "loadPower": "load_power",
}

# Dashboard-friendly canonical sensors that are intentionally exposed.
# AC Output Power is omitted because Load Power represents the same active load
# for this inverter. Grid Feed-in is also omitted because this installation uses
# Grid Import Power as its useful grid-flow sensor.
IMPORTANT_CANONICAL_SENSOR_KEYS: tuple[str, ...] = (
    "pvInputPower",
    "loadPower",
    "gridPower",
    "batteryPower",
    "batteryVoltage",
    "batterySOC",
    "batteryChargingCurrent",
    "batteryDischargeCurrent",
)

# Raw protocol fields worth exposing in addition to the canonical sensors.
# Together with IMPORTANT_CANONICAL_SENSOR_KEYS this produces the approved
# 20-sensor inverter telemetry set.
IMPORTANT_PROTOCOL_SENSOR_KEYS: tuple[str, ...] = (
    "pvVoltage",
    "pvCurrent",
    "outputVoltage",
    "outputLoadPercent",
    "acInputVoltage",
    "mode",
    "inverterTemperature",
    "pvGeneratedEnergyOfDay",
    "tqfMonthlyElectricityGeneration",
    "tqfYearlyElectricityGeneration",
    "pvGeneratedEnergyOfTotal",
    "statusCode",
)

# These canonical measurements already represent their corresponding raw keys,
# so the raw protocol copies should never be created as extra entities.
_CANONICAL_RAW_KEYS = {
    "batteryVoltage",
    "batteryChargingCurrent",
    "batteryDischargeCurrent",
}


def _normalise_settings(payload: Any) -> dict[str, Any]:
    """Flatten the known settings-cache and batch-read response shapes."""
    if not isinstance(payload, dict):
        return {}

    current = payload
    for key in ("configAttributeStates", "targetConfig"):
        nested = current.get(key)
        if isinstance(nested, dict):
            current = nested
            break

    result: dict[str, Any] = {}
    for key, entry in current.items():
        if isinstance(entry, dict):
            item = dict(entry)
            item.setdefault("key", key)
            if "value" not in item and "v" in item:
                item["value"] = item.get("v")
            result[key] = item
        else:
            result[key] = {"key": key, "value": entry}
    return result


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed.is_integer():
        return int(parsed)
    return round(parsed, 4)


def _apply_measurement_metadata(entity: SensorEntity, unit: str, key: str) -> None:
    """Apply HA device/state classes where the protocol unit is unambiguous."""
    if not unit:
        return

    entity._attr_native_unit_of_measurement = unit

    if unit in ("W", "kW"):
        entity._attr_device_class = SensorDeviceClass.POWER
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit == "kWh":
        entity._attr_device_class = SensorDeviceClass.ENERGY
        entity._attr_state_class = SensorStateClass.TOTAL_INCREASING
    elif unit == "A":
        entity._attr_device_class = SensorDeviceClass.CURRENT
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit in ("V", "mV"):
        entity._attr_device_class = SensorDeviceClass.VOLTAGE
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit == "Hz":
        entity._attr_device_class = SensorDeviceClass.FREQUENCY
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit == "°C":
        entity._attr_device_class = SensorDeviceClass.TEMPERATURE
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit == "VA":
        entity._attr_device_class = SensorDeviceClass.APPARENT_POWER
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit == "%":
        if "battery" in key.lower() or "soc" in key.lower():
            entity._attr_device_class = SensorDeviceClass.BATTERY
        entity._attr_state_class = SensorStateClass.MEASUREMENT
    elif unit not in ("h", "min", "day"):
        entity._attr_state_class = SensorStateClass.MEASUREMENT


def _telemetry_attributes(coordinator) -> dict[str, Any]:
    """Expose cloud polling diagnostics without separate debug entities."""
    time_series = (coordinator.data or {}).get("time_series") or {}
    attributes: dict[str, Any] = {
        "telemetry_source": time_series.get("source"),
        "polled_at": time_series.get("polled_at"),
        "cloud_sample_time": time_series.get("cloud_sample_time"),
    }
    if time_series.get("live_endpoint"):
        attributes["live_endpoint"] = time_series.get("live_endpoint")
    if time_series.get("live_error"):
        attributes["live_error"] = time_series.get("live_error")
    return attributes


def _remove_obsolete_telemetry_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    station_id: str,
    device_ids: list[str],
) -> None:
    """Remove previously-created raw/duplicate telemetry entities from registry.

    Parameter-setting sensors are deliberately excluded from this cleanup. The
    number/select/switch control platforms are separate and are not touched.
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)

    allowed_protocol = set(IMPORTANT_PROTOCOL_SENSOR_KEYS)
    obsolete_canonical = {"acOutputActivePower", "feedInPower"}
    obsolete_station_monthly = {
        "monthly_pv_generated",
        "monthly_grid_import",
        "monthly_total_consumption",
        "monthly_solar_percentage",
    }

    for registry_entry in entries:
        if registry_entry.domain != "sensor":
            continue

        unique_id = registry_entry.unique_id
        should_remove = False

        for device_id in device_ids:
            protocol_prefix = f"{DOMAIN}_{station_id}_{device_id}_protocol_"
            if unique_id.startswith(protocol_prefix):
                key = unique_id[len(protocol_prefix) :]
                if key not in allowed_protocol:
                    should_remove = True
                break

            for key in obsolete_canonical:
                if unique_id == f"{DOMAIN}_{station_id}_{device_id}_{key}":
                    should_remove = True
                    break
            if should_remove:
                break

        if not should_remove:
            for key in obsolete_station_monthly:
                if unique_id == f"{DOMAIN}_{station_id}_{key}":
                    should_remove = True
                    break

        if should_remove:
            registry.async_remove(registry_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    station_id: str = data["station_id"]
    device_coordinators = data["device_coordinators"]

    entities: list[SensorEntity] = []

    for device_id, coordinator in device_coordinators.items():
        device_name = (coordinator.device_meta or {}).get("name") or device_id

        for key in IMPORTANT_CANONICAL_SENSOR_KEYS:
            definition = SENSOR_DEFINITIONS.get(key)
            if not definition:
                continue
            entities.append(
                SolarOfThingsCanonicalSensor(
                    coordinator,
                    station_id,
                    device_id,
                    device_name,
                    key,
                    definition,
                )
            )

        for key in IMPORTANT_PROTOCOL_SENSOR_KEYS:
            if key in _CANONICAL_RAW_KEYS:
                continue
            metadata = PROTOCOL_SCHEMA.get(key)
            if not metadata:
                continue
            entities.append(
                SolarOfThingsProtocolSensor(
                    coordinator,
                    station_id,
                    device_id,
                    device_name,
                    key,
                    metadata,
                )
            )

        # Keep every parameter-setting readback. These are intentionally not
        # part of the telemetry pruning because they support inverter setup and
        # make the writable controls easy to verify.
        settings = _normalise_settings((coordinator.data or {}).get("settings"))
        for key, metadata in settings.items():
            entities.append(
                SolarOfThingsSettingSensor(
                    coordinator,
                    station_id,
                    device_id,
                    device_name,
                    key,
                    metadata,
                )
            )

    _remove_obsolete_telemetry_entities(
        hass,
        entry,
        station_id,
        list(device_coordinators),
    )

    async_add_entities(entities)


class _DeviceSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, station_id: str, device_id: str, device_name: str) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._device_id = device_id
        self._device_name = device_name
        self._attr_has_entity_name = True

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Siseli",
            "model": (self.coordinator.data.get("device_meta") or {}).get("model")
            if self.coordinator.data
            else None,
            "via_device": (DOMAIN, self._station_id),
        }


class SolarOfThingsCanonicalSensor(_DeviceSensor):
    def __init__(
        self,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
        sensor_key: str,
        definition: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, station_id, device_id, device_name)
        self._sensor_key = sensor_key
        self._attr_translation_key = _TRANSLATION_KEYS.get(sensor_key)
        if not self._attr_translation_key:
            self._attr_name = definition["name"]
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_{sensor_key}"
        self._attr_icon = definition.get("icon")
        _apply_measurement_metadata(self, definition.get("unit", ""), sensor_key)

    @property
    def native_value(self):
        time_series = (self.coordinator.data or {}).get("time_series") or {}
        canonical = time_series.get("canonical") or {}
        return _number(canonical.get(self._sensor_key))

    @property
    def extra_state_attributes(self):
        return _telemetry_attributes(self.coordinator)


class SolarOfThingsProtocolSensor(_DeviceSensor):
    def __init__(
        self,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
        sensor_key: str,
        metadata: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, station_id, device_id, device_name)
        self._sensor_key = sensor_key
        self._metadata = metadata
        self._attr_name = metadata.get("name") or sensor_key
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_protocol_{sensor_key}"

        if metadata.get("type") == "Numeric":
            _apply_measurement_metadata(self, metadata.get("unit") or "", sensor_key)

    @property
    def native_value(self):
        time_series = (self.coordinator.data or {}).get("time_series") or {}
        raw = time_series.get("raw") or {}
        value = raw.get(self._sensor_key)
        if value is None:
            return None

        if self._metadata.get("type") == "Numeric":
            return _number(value)

        enum_map = self._metadata.get("enum") or {}
        if enum_map:
            return enum_map.get(str(value), str(value))
        return str(value)

    @property
    def extra_state_attributes(self):
        attributes = {
            "api_key": self._sensor_key,
            "protocol_group": self._metadata.get("group"),
            **_telemetry_attributes(self.coordinator),
        }
        enum_map = self._metadata.get("enum") or {}
        if enum_map:
            raw = (
                (((self.coordinator.data or {}).get("time_series") or {}).get("raw") or {})
                .get(self._sensor_key)
            )
            attributes["raw_value"] = raw
        return attributes


class SolarOfThingsSettingSensor(_DeviceSensor):
    def __init__(
        self,
        coordinator,
        station_id: str,
        device_id: str,
        device_name: str,
        setting_key: str,
        metadata: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, station_id, device_id, device_name)
        self._setting_key = setting_key
        self._initial_metadata = metadata
        name = metadata.get("nameDisplay") or metadata.get("name") or setting_key
        self._attr_name = f"Parameter {name}"
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_setting_{setting_key}"
        unit = metadata.get("unit") or ""
        value_type = metadata.get("valueTypeDict")
        if value_type == "Numeric" or isinstance(metadata.get("value"), (int, float)):
            _apply_measurement_metadata(self, unit, setting_key)
            self._attr_state_class = None

    def _entry(self) -> dict[str, Any] | None:
        settings = _normalise_settings((self.coordinator.data or {}).get("settings"))
        entry = settings.get(self._setting_key)
        return entry if isinstance(entry, dict) else None

    @property
    def native_value(self):
        entry = self._entry()
        if not entry:
            return None
        value_type = entry.get("valueTypeDict") or self._initial_metadata.get("valueTypeDict")
        value = entry.get("value")
        if value_type == "Numeric" or isinstance(value, (int, float)):
            return _number(value)
        return entry.get("valueDisplay") or (str(value) if value is not None else None)

    @property
    def extra_state_attributes(self):
        entry = self._entry() or self._initial_metadata
        return {
            "api_key": self._setting_key,
            "raw_value": entry.get("value"),
            "value_type": entry.get("valueTypeDict"),
            "read_only": True,
        }
