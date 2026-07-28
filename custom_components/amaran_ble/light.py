"""Light platform for Amaran BLE."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .controller import AmaranBleController
from .models import AmaranLightConfig
from .telink import TelinkStatus

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Amaran BLE light entities."""
    controller: AmaranBleController = hass.data[DOMAIN][entry.entry_id]
    entities = [
        AmaranBleLight(controller, light_config)
        for light_config in controller.config.lights
    ]
    _LOGGER.info("Adding %s Amaran BLE light entity/entities", len(entities))
    async_add_entities(entities)


class AmaranBleLight(LightEntity, RestoreEntity):
    """Representation of one Amaran BLE Mesh light."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, controller: AmaranBleController, config: AmaranLightConfig) -> None:
        """Initialize the light."""
        self._controller = controller
        self._config = config
        self._attr_unique_id = f"amaran_ble_{config.mac.lower().replace(':', '')}_{config.mesh_address:04x}"
        self._attr_min_color_temp_kelvin = config.min_kelvin
        self._attr_max_color_temp_kelvin = config.max_kelvin
        color_modes = {ColorMode.COLOR_TEMP}
        if config.supports_hs:
            color_modes.add(ColorMode.HS)
        self._attr_supported_color_modes = color_modes
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_is_on = None
        self._attr_available = False
        self._attr_brightness = 255
        self._attr_color_temp_kelvin = min(max(4500, config.min_kelvin), config.max_kelvin)
        self._attr_hs_color = (0.0, 100.0)

    async def async_added_to_hass(self) -> None:
        """Restore Home Assistant's last known state after startup."""
        self._attr_available = self._controller.is_light_available(self._config.mac)
        self.async_on_remove(
            self._controller.async_track_availability(
                self._config.mac,
                self._handle_availability,
            )
        )
        self.async_on_remove(
            self._controller.async_track_status(
                self._config.mesh_address,
                self._handle_status,
            )
        )

        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.state == STATE_ON:
                self._attr_is_on = True
            elif last_state.state == STATE_OFF:
                self._attr_is_on = False
            if (brightness := last_state.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._attr_brightness = int(brightness)
            if (kelvin := last_state.attributes.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
                self._attr_color_temp_kelvin = max(
                    self._config.min_kelvin,
                    min(self._config.max_kelvin, int(kelvin)),
                )
                self._attr_color_mode = ColorMode.COLOR_TEMP
            if (
                self._config.supports_hs
                and (hs_color := last_state.attributes.get(ATTR_HS_COLOR)) is not None
            ):
                hue, saturation = hs_color
                self._attr_hs_color = (float(hue), float(saturation))
                self._attr_color_mode = ColorMode.HS
        self._ensure_supported_color_mode()
        if (status := self._controller.last_status(self._config.mesh_address)) is not None:
            self._handle_status(status)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, f"{self._config.mesh_address:04x}")},
            "name": self._config.name,
            "manufacturer": "amaran",
            "model": "BLE Mesh fixture",
            "connections": {("mac", self._config.mac)},
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        _LOGGER.info("Amaran BLE turn_on requested for %s with %s", self._config.mac, kwargs)
        brightness = int(kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255))
        intensity = max(1, min(1000, round(brightness * 1000 / 255)))

        status: TelinkStatus | None = None
        # A delayed status report can briefly leave HA showing "on" after the
        # fixture has already turned off. Always send an explicit Power On
        # before brightness/CCT/HS commands so stale state cannot suppress it.
        if kwargs:
            status = await self._controller.async_turn_on(
                self._config.mac,
                self._config.mesh_address,
            )

        if ATTR_HS_COLOR in kwargs and self._config.supports_hs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            status = await self._controller.async_set_hsi(
                self._config.mac,
                self._config.mesh_address,
                round(hue),
                round(saturation),
                intensity,
            )
            self._attr_hs_color = (float(hue), float(saturation))
            self._attr_color_mode = ColorMode.HS
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            kelvin = max(self._config.min_kelvin, min(self._config.max_kelvin, kelvin))
            status = await self._controller.async_set_cct(
                self._config.mac,
                self._config.mesh_address,
                kelvin,
                intensity,
            )
            self._attr_color_temp_kelvin = kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ATTR_BRIGHTNESS in kwargs:
            status = await self._controller.async_set_brightness(
                self._config.mac,
                self._config.mesh_address,
                intensity,
            )
        else:
            status = await self._controller.async_turn_on(
                self._config.mac,
                self._config.mesh_address,
            )

        confirmed = await self._confirm_status_after_command(status)
        if not confirmed:
            self._attr_available = self._controller.is_light_available(self._config.mac)
            self._attr_is_on = True
            self._attr_brightness = brightness
            self._ensure_supported_color_mode()
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        _LOGGER.info("Amaran BLE turn_off requested for %s", self._config.mac)
        status = await self._controller.async_turn_off(
            self._config.mac,
            self._config.mesh_address,
        )
        confirmed = await self._confirm_status_after_command(status)
        if not confirmed:
            self._attr_available = self._controller.is_light_available(self._config.mac)
            self._attr_is_on = False
            self._ensure_supported_color_mode()
            self.async_write_ha_state()

    async def _confirm_status_after_command(
        self,
        status: TelinkStatus | None = None,
    ) -> bool:
        """Use an immediate report when present; otherwise update optimistically."""
        if status is not None:
            return True
        _LOGGER.debug(
            "Amaran BLE command to %s was sent; notifications and polling will "
            "reconcile the optimistic state",
            self._config.mac,
        )
        return False

    def _handle_availability(self, available: bool) -> None:
        """Handle BLE availability updates."""
        self._attr_available = available
        self._ensure_supported_color_mode()
        self.async_write_ha_state()

    def _handle_status(self, status: TelinkStatus) -> None:
        """Handle decoded fixture status updates."""
        self._attr_available = True
        self._attr_is_on = status.on
        self._attr_brightness = max(1, min(255, round(status.intensity * 255 / 1000)))
        if status.is_hs and self._config.supports_hs:
            self._attr_color_mode = ColorMode.HS
            self._attr_hs_color = (float(status.hue), float(status.saturation))
        elif status.cct_kelvin:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = max(
                self._config.min_kelvin,
                min(self._config.max_kelvin, status.cct_kelvin),
            )
        self._ensure_supported_color_mode()
        self.async_write_ha_state()

    def _ensure_supported_color_mode(self) -> None:
        """Normalize restored or stale state to this fixture's capabilities."""
        if self._attr_color_mode not in self._attr_supported_color_modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
