"""Sensor platform for the inverter-specific Solar of Things fork."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSOR_DEFINITIONS
from .telemetry import PROTOCOL_SCHEMA

_TRANSLATION_KEYS: dict[str, str] = {
    "pvInputPower": "pv_input_power",
    "acOutputActivePower": "ac_output_active_power",
    "batteryDischargeCurrent": "battery_discharge_current",
    "batteryChargingCurrent": "battery_charging_current",
    "batteryVoltage": "battery_voltage",
    "batteryPower": "battery_power",
    "batterySOC": "battery_soc",
    "feedInPower": "feed_in_power",
    "gridPower": "grid_power",
    "loadPower": "load_power",
    "monthly_pv_generated": "monthly_pv_generated",
    "monthly_grid_import": "monthly_grid_import",
    "monthly_total_consumption": "monthly_total_consumption",
    "monthly_solar_percentage": "monthly_solar_percentage",
}

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
    """Expose polling diagnostics without creating high-churn debug entities."""
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    station_id: str = data["station_id"]
    device_coordinators = data["device_coordinators"]
    station_coordinator = data["station_coordinator"]

    entities: list[SensorEntity] = []

    for device_id, coordinator in device_coordinators.items():
        device_name = (coordinator.device_meta or {}).get("name") or device_id

        for key, definition in SENSOR_DEFINITIONS.items():
            if key.startswith("monthly_"):
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

        for key, metadata in PROTOCOL_SCHEMA.items():
            if key in _CANONICAL_RAW_KEYS:
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

    if station_coordinator:
        for key, definition in SENSOR_DEFINITIONS.items():
            if key.startswith("monthly_"):
                entities.append(
                    SolarOfThingsStationMonthlySensor(
                        station_coordinator,
                        station_id,
                        key,
                        definition,
                    )
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
        super().__init__(coordinator)
        self._station_id = station_id
        self._device_id = device_id
        self._device_name = device_name
        self._sensor_key = sensor_key
        self._metadata = metadata
        self._attr_has_entity_name = True
        self._attr_name = metadata.get("name") or sensor_key
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{device_id}_protocol_{sensor_key}"

        if metadata.get("type") == "Numeric":
            _apply_measurement_metadata(self, metadata.get("unit") or "", sensor_key)

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


class SolarOfThingsStationMonthlySensor(CoordinatorEntity, SensorEntity):
    def __init__(
        self,
        coordinator,
        station_id: str,
        sensor_key: str,
        definition: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        self._sensor_key = sensor_key
        self._attr_has_entity_name = True
        self._attr_translation_key = _TRANSLATION_KEYS.get(sensor_key)
        if not self._attr_translation_key:
            self._attr_name = definition["name"]
        self._attr_unique_id = f"{DOMAIN}_{station_id}_{sensor_key}"
        self._attr_icon = definition.get("icon")
        _apply_measurement_metadata(self, definition.get("unit", ""), sensor_key)
        if definition.get("unit") == "kWh":
            self._attr_state_class = SensorStateClass.TOTAL

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._station_id)},
            "name": f"Solar Station {self._station_id}",
            "manufacturer": "Siseli",
            "model": "Station",
        }

    @property
    def native_value(self):
        monthly = (self.coordinator.data or {}).get("monthly") or {}
        return _number(monthly.get(self._sensor_key))
