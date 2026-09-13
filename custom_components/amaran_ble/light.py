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

from .color_state import (
    CCT_STEP_KELVIN,
    FixtureColorState,
    quantize_kelvin,
)
from .const import ATTR_GREEN_MAGENTA, DOMAIN
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
        color_modes: set[ColorMode] = set()
        if config.supports_cct:
            color_modes.add(ColorMode.COLOR_TEMP)
            self._attr_min_color_temp_kelvin = config.min_kelvin
            self._attr_max_color_temp_kelvin = config.max_kelvin
        if config.supports_hs:
            color_modes.add(ColorMode.HS)
        if not color_modes:
            # A fixed-white fixture only accepts power and brightness.
            color_modes.add(ColorMode.BRIGHTNESS)
        self._attr_supported_color_modes = color_modes
        if config.supports_cct:
            self._default_color_mode = ColorMode.COLOR_TEMP
        elif config.supports_hs:
            self._default_color_mode = ColorMode.HS
        else:
            self._default_color_mode = ColorMode.BRIGHTNESS
        self._attr_color_mode = self._default_color_mode
        self._attr_is_on = None
        self._attr_available = False
        self._attr_brightness = 255
        fixture_state = controller.fixture_state(config.mesh_address)
        self._attr_color_temp_kelvin = (
            fixture_state.kelvin if config.supports_cct else None
        )
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
            self._controller.async_track_fixture_state(
                self._config.mesh_address,
                self._handle_fixture_state,
            )
        )

        restored_state = self._controller.fixture_state(
            self._config.mesh_address
        )
        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.state == STATE_ON:
                restored_state.apply_power(True)
            elif last_state.state == STATE_OFF:
                restored_state.apply_power(False)
            if (brightness := last_state.attributes.get(ATTR_BRIGHTNESS)) is not None:
                restored_state.apply_intensity(
                    int(brightness) * 1000 / 255
                )
            gm = int(
                last_state.attributes.get(
                    ATTR_GREEN_MAGENTA,
                    restored_state.gm,
                )
            )
            if (kelvin := last_state.attributes.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
                restored_state.apply_cct(
                    int(kelvin),
                    restored_state.intensity,
                    gm,
                )
            if (
                self._config.supports_hs
                and last_state.attributes.get("color_mode") == ColorMode.HS
                and (hs_color := last_state.attributes.get(ATTR_HS_COLOR)) is not None
            ):
                hue, saturation = hs_color
                restored_state.apply_hsi(
                    hue,
                    saturation,
                    restored_state.intensity,
                )
            self._controller.seed_fixture_state(
                self._config.mesh_address,
                restored_state,
            )
        self._handle_fixture_state(
            self._controller.fixture_state(self._config.mesh_address)
        )

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

    @property
    def extra_state_attributes(self) -> dict[str, int | float]:
        """Expose Amaran-specific color metadata."""
        state = self._controller.fixture_state(self._config.mesh_address)
        attributes: dict[str, int | float] = {
            ATTR_GREEN_MAGENTA: state.gm,
            "hsi_intensity": state.intensity / 10,
        }
        if self._config.supports_cct:
            attributes["color_temperature_step_kelvin"] = CCT_STEP_KELVIN
        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        _LOGGER.info("Amaran BLE turn_on requested for %s with %s", self._config.mac, kwargs)
        brightness = int(kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255))
        intensity = max(1, min(1000, round(brightness * 1000 / 255)))
        fixture_state = self._controller.fixture_state(
            self._config.mesh_address
        )

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
            hue = round(hue)
            saturation = round(saturation)
            status = await self._controller.async_set_hsi(
                self._config.mac,
                self._config.mesh_address,
                hue,
                saturation,
                intensity,
            )
            self._attr_hs_color = (float(hue), float(saturation))
            self._attr_color_mode = ColorMode.HS
        elif ATTR_COLOR_TEMP_KELVIN in kwargs and self._config.supports_cct:
            kelvin = quantize_kelvin(
                kwargs[ATTR_COLOR_TEMP_KELVIN],
                self._config.min_kelvin,
                self._config.max_kelvin,
            )
            status = await self._controller.async_set_cct(
                self._config.mac,
                self._config.mesh_address,
                kelvin,
                intensity,
                fixture_state.gm,
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

    def _handle_fixture_state(self, state: FixtureColorState) -> None:
        """Handle shared physical or optimistic fixture state."""
        if state.on is not None:
            self._attr_is_on = state.on
        self._attr_brightness = max(
            1,
            min(255, round(state.intensity * 255 / 1000)),
        )
        if state.is_hs and self._config.supports_hs:
            self._attr_color_mode = ColorMode.HS
            self._attr_hs_color = (
                float(state.hue),
                float(state.saturation),
            )
        elif self._config.supports_cct:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = state.kelvin
        else:
            self._attr_color_mode = self._default_color_mode
        self._ensure_supported_color_mode()
        self.async_write_ha_state()

    def _ensure_supported_color_mode(self) -> None:
        """Normalize restored or stale state to this fixture's capabilities."""
        if self._attr_color_mode not in self._attr_supported_color_modes:
            self._attr_color_mode = self._default_color_mode
