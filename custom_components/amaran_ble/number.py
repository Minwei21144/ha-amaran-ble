"""Advanced numeric color controls for Amaran BLE fixtures."""

from __future__ import annotations

from enum import Enum
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .color_state import (
    CCT_STEP_KELVIN,
    GM_MAX,
    GM_MIN,
    HUE_MAX,
    HUE_MIN,
    INTENSITY_MAX,
    SATURATION_MAX,
    SATURATION_MIN,
    FixtureColorState,
    quantize_kelvin,
)
from .const import DOMAIN
from .controller import AmaranBleController
from .models import AmaranLightConfig


class AdvancedControl(str, Enum):
    """Supported numeric color controls."""

    # Keep the legacy translation/unique-id suffix for existing installations.
    BRIGHTNESS = "hsi_intensity"
    COLOR_TEMPERATURE = "color_temperature"
    GREEN_MAGENTA = "green_magenta"
    HUE = "hue"
    SATURATION = "saturation"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up advanced numeric controls for configured fixtures."""
    controller: AmaranBleController = hass.data[DOMAIN][entry.entry_id]
    entities: list[AmaranBleColorNumber] = []
    for config in controller.config.lights:
        entities.extend(
            (
                AmaranBleColorNumber(
                    controller,
                    config,
                    AdvancedControl.BRIGHTNESS,
                ),
                AmaranBleColorNumber(
                    controller,
                    config,
                    AdvancedControl.COLOR_TEMPERATURE,
                ),
            )
        )
        if config.supports_gm:
            entities.append(
                AmaranBleColorNumber(
                    controller,
                    config,
                    AdvancedControl.GREEN_MAGENTA,
                )
            )
        if config.supports_hs:
            entities.extend(
                AmaranBleColorNumber(controller, config, control)
                for control in (
                    AdvancedControl.HUE,
                    AdvancedControl.SATURATION,
                )
            )
    async_add_entities(entities)


class AmaranBleColorNumber(NumberEntity):
    """One Amaran numeric control."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        controller: AmaranBleController,
        config: AmaranLightConfig,
        control: AdvancedControl,
    ) -> None:
        """Initialize a fixture control."""
        self._controller = controller
        self._config = config
        self._control = control
        self._attr_translation_key = control.value
        self._attr_unique_id = (
            f"amaran_ble_{config.mac.lower().replace(':', '')}_"
            f"{config.mesh_address:04x}_{control.value}"
        )
        self._attr_available = False
        self._configure_range()
        self._attr_native_value = self._value_from_state(
            controller.fixture_state(config.mesh_address)
        )

    def _configure_range(self) -> None:
        """Configure the native range and display unit."""
        if self._control is AdvancedControl.BRIGHTNESS:
            self._attr_native_min_value = 0.1
            self._attr_native_max_value = INTENSITY_MAX / 10
            self._attr_native_step = 0.1
            self._attr_suggested_display_precision = 1
            self._attr_native_unit_of_measurement = "%"
            self._attr_icon = "mdi:brightness-6"
        elif self._control is AdvancedControl.COLOR_TEMPERATURE:
            self._attr_native_min_value = self._config.min_kelvin
            self._attr_native_max_value = self._config.max_kelvin
            self._attr_native_step = CCT_STEP_KELVIN
            self._attr_suggested_display_precision = 0
            self._attr_native_unit_of_measurement = "K"
            self._attr_icon = "mdi:thermometer"
        elif self._control is AdvancedControl.GREEN_MAGENTA:
            self._attr_native_min_value = GM_MIN
            self._attr_native_max_value = GM_MAX
            self._attr_native_step = 1
            self._attr_suggested_display_precision = 0
            self._attr_icon = "mdi:circle-half-full"
        elif self._control is AdvancedControl.HUE:
            self._attr_native_min_value = HUE_MIN
            self._attr_native_max_value = HUE_MAX
            self._attr_native_step = 1
            self._attr_suggested_display_precision = 0
            self._attr_native_unit_of_measurement = "°"
            self._attr_icon = "mdi:palette"
        elif self._control is AdvancedControl.SATURATION:
            self._attr_native_min_value = SATURATION_MIN
            self._attr_native_max_value = SATURATION_MAX
            self._attr_native_step = 1
            self._attr_suggested_display_precision = 0
            self._attr_native_unit_of_measurement = "%"
            self._attr_icon = "mdi:invert-colors"

    @property
    def device_info(self) -> dict[str, Any]:
        """Group the control with its fixture."""
        return {
            "identifiers": {(DOMAIN, f"{self._config.mesh_address:04x}")},
            "name": self._config.name,
            "manufacturer": "amaran",
            "model": "BLE Mesh fixture",
            "connections": {("mac", self._config.mac)},
        }

    @property
    def native_value(self) -> float | int:
        """Return the latest shared fixture value."""
        return self._attr_native_value

    def _value_from_state(
        self,
        state: FixtureColorState,
    ) -> float | int:
        """Extract this entity's channel from a shared state snapshot."""
        if self._control is AdvancedControl.BRIGHTNESS:
            return state.intensity / 10
        if self._control is AdvancedControl.COLOR_TEMPERATURE:
            return state.kelvin
        if self._control is AdvancedControl.GREEN_MAGENTA:
            return state.gm
        if self._control is AdvancedControl.HUE:
            return state.hue
        if self._control is AdvancedControl.SATURATION:
            return state.saturation
        raise ValueError(f"Unsupported Amaran control: {self._control}")

    async def async_added_to_hass(self) -> None:
        """Update when the shared fixture state changes."""
        self._attr_available = self._controller.is_light_available(
            self._config.mac
        )
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

    async def async_set_native_value(self, value: float) -> None:
        """Set one value while preserving companion channels and color mode."""
        state = self._controller.fixture_state(
            self._config.mesh_address
        )
        intensity = max(1, state.intensity)

        await self._controller.async_turn_on(
            self._config.mac,
            self._config.mesh_address,
        )
        if self._control is AdvancedControl.BRIGHTNESS:
            await self._controller.async_set_brightness(
                self._config.mac,
                self._config.mesh_address,
                max(1, min(INTENSITY_MAX, round(value * 10))),
            )
        elif self._control is AdvancedControl.COLOR_TEMPERATURE:
            kelvin = quantize_kelvin(
                value,
                self._config.min_kelvin,
                self._config.max_kelvin,
            )
            await self._controller.async_set_cct(
                self._config.mac,
                self._config.mesh_address,
                kelvin,
                intensity,
                state.gm,
            )
        elif self._control is AdvancedControl.GREEN_MAGENTA:
            await self._controller.async_set_cct(
                self._config.mac,
                self._config.mesh_address,
                state.kelvin,
                intensity,
                round(value),
            )
        elif self._control is AdvancedControl.HUE:
            await self._controller.async_set_hsi(
                self._config.mac,
                self._config.mesh_address,
                round(value),
                state.saturation,
                intensity,
            )
        elif self._control is AdvancedControl.SATURATION:
            await self._controller.async_set_hsi(
                self._config.mac,
                self._config.mesh_address,
                state.hue,
                round(value),
                intensity,
            )
        self._handle_fixture_state(
            self._controller.fixture_state(self._config.mesh_address)
        )

    def _handle_availability(self, available: bool) -> None:
        """Handle fixture reachability updates."""
        if self._attr_available == available:
            return
        self._attr_available = available
        self.async_write_ha_state()

    def _handle_fixture_state(
        self,
        state: FixtureColorState,
    ) -> None:
        """Publish a changed shared color value."""
        value = self._value_from_state(state)
        if self._attr_native_value == value:
            return
        self._attr_native_value = value
        self.async_write_ha_state()
