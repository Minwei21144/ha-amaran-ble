"""Switch platform for Amaran BLE control ownership."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .controller import AmaranBleController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Amaran BLE ownership switch."""
    controller: AmaranBleController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AmaranBleControlSwitch(controller, entry)])


class AmaranBleControlSwitch(SwitchEntity, RestoreEntity):
    """Switch that lets Home Assistant hold or release Amaran BLE control."""

    _attr_has_entity_name = True
    _attr_translation_key = "control"
    _attr_icon = "mdi:connection"

    def __init__(self, controller: AmaranBleController, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_control"
        self._attr_is_on = controller.control_enabled

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, "mesh_control")},
            "name": "Amaran BLE",
            "manufacturer": "amaran",
            "model": "BLE Mesh control",
        }

    async def async_added_to_hass(self) -> None:
        """Restore the last control ownership preference."""
        self.async_on_remove(
            self._controller.async_track_control_enabled(
                self._handle_control_enabled,
            )
        )
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == STATE_OFF:
            await self._controller.async_set_control_enabled(False)
        self._attr_is_on = self._controller.control_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let Home Assistant connect to and control the Amaran mesh."""
        await self._controller.async_set_control_enabled(True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release Amaran BLE control for the vendor apps."""
        await self._controller.async_set_control_enabled(False)
        self._attr_is_on = False
        self.async_write_ha_state()

    def _handle_control_enabled(self, enabled: bool) -> None:
        """Handle controller switch changes."""
        self._attr_is_on = enabled
        self.async_write_ha_state()
