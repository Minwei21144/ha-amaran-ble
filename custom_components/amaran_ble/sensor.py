"""Diagnostic sensor platform for Amaran BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import AmaranBleController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Amaran BLE diagnostic sensors."""
    controller: AmaranBleController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AmaranBleConnectionSensor(controller, entry)])


class AmaranBleConnectionSensor(SensorEntity):
    """Expose mesh proxy connection state for troubleshooting."""

    _attr_has_entity_name = True
    _attr_translation_key = "connection"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: AmaranBleController, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, "mesh_control")},
            "name": "Amaran BLE",
            "manufacturer": "amaran",
            "model": "BLE Mesh control",
        }

    @property
    def native_value(self) -> str:
        """Return the current connection state."""
        return str(self._controller.diagnostic_state()["state"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional connection diagnostics."""
        # Home Assistant limits recorder state attributes to 16 KiB. Keep the
        # entity compact while diagnostics downloads retain the full histories.
        state = self._controller.diagnostic_state(compact=True)
        return {
            key: value
            for key, value in state.items()
            if key != "state"
        }

    async def async_added_to_hass(self) -> None:
        """Update when controller state changes."""
        self.async_on_remove(
            self._controller.async_track_diagnostics(
                self.async_write_ha_state,
            )
        )
