"""Offline checks for restored Amaran light state."""

from __future__ import annotations

from enum import Enum
import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest


def _install_homeassistant_light_stubs() -> None:
    """Install the small HA light surface needed by these tests."""
    homeassistant = sys.modules.setdefault("homeassistant", ModuleType("homeassistant"))
    components = sys.modules.setdefault(
        "homeassistant.components",
        ModuleType("homeassistant.components"),
    )
    helpers = sys.modules.setdefault(
        "homeassistant.helpers",
        ModuleType("homeassistant.helpers"),
    )

    light = ModuleType("homeassistant.components.light")

    class ColorMode(str, Enum):
        COLOR_TEMP = "color_temp"
        HS = "hs"

    class LightEntity:
        def async_on_remove(self, callback: object) -> None:
            self._remove_callbacks = getattr(self, "_remove_callbacks", [])
            self._remove_callbacks.append(callback)

        def async_write_ha_state(self) -> None:
            self._write_count = getattr(self, "_write_count", 0) + 1

    light.ATTR_BRIGHTNESS = "brightness"
    light.ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
    light.ATTR_HS_COLOR = "hs_color"
    light.ColorMode = ColorMode
    light.LightEntity = LightEntity

    bluetooth = ModuleType("homeassistant.components.bluetooth")
    bluetooth.async_ble_device_from_address = lambda *_args, **_kwargs: None
    bluetooth.async_last_service_info = lambda *_args, **_kwargs: None
    bluetooth.BluetoothScanningMode = SimpleNamespace(ACTIVE="active")

    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object

    const = ModuleType("homeassistant.const")
    class Platform(str, Enum):
        LIGHT = "light"
        SWITCH = "switch"
        SENSOR = "sensor"

    const.Platform = Platform
    const.STATE_OFF = "off"
    const.STATE_ON = "on"

    core = sys.modules.setdefault(
        "homeassistant.core",
        ModuleType("homeassistant.core"),
    )
    core.HomeAssistant = getattr(core, "HomeAssistant", object)

    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    restore_state = ModuleType("homeassistant.helpers.restore_state")

    class RestoreEntity:
        async def async_get_last_state(self) -> object:
            return getattr(self, "_test_last_state", None)

    restore_state.RestoreEntity = RestoreEntity

    components.light = light
    components.bluetooth = bluetooth
    helpers.entity_platform = entity_platform
    helpers.restore_state = restore_state
    homeassistant.components = components
    homeassistant.helpers = helpers

    sys.modules["homeassistant.components.light"] = light
    sys.modules["homeassistant.components.bluetooth"] = bluetooth
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform
    sys.modules["homeassistant.helpers.restore_state"] = restore_state


_install_homeassistant_light_stubs()
created_controller_stub = False
if "custom_components.amaran_ble.controller" not in sys.modules:
    controller_stub = ModuleType("custom_components.amaran_ble.controller")
    controller_stub.AmaranBleController = object
    sys.modules["custom_components.amaran_ble.controller"] = controller_stub
    created_controller_stub = True
light_module = importlib.import_module("custom_components.amaran_ble.light")
if created_controller_stub:
    del sys.modules["custom_components.amaran_ble.controller"]
models_module = importlib.import_module("custom_components.amaran_ble.models")


class _FakeController:
    def is_light_available(self, _mac: str) -> bool:
        return False

    def async_track_availability(
        self,
        _mac: str,
        _callback: object,
    ) -> object:
        return lambda: None

    def async_track_status(
        self,
        _mesh_address: int,
        _callback: object,
    ) -> object:
        return lambda: None

    def last_status(self, _mesh_address: int) -> None:
        return None


def _fixture(*, supports_hs: bool) -> object:
    config = models_module.AmaranLightConfig(
        name="Test fixture",
        mac="AA:BB:CC:11:22:33",
        mesh_address=3,
        min_kelvin=2700,
        max_kelvin=6500,
        supports_hs=supports_hs,
        supports_gm=supports_hs,
    )
    return light_module.AmaranBleLight(_FakeController(), config)


class LightRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_bicolor_fixture_ignores_restored_hs_mode(self) -> None:
        entity = _fixture(supports_hs=False)
        entity._test_last_state = SimpleNamespace(
            state="on",
            attributes={
                "brightness": 64,
                "color_temp_kelvin": 8000,
                "hs_color": (240.0, 100.0),
            },
        )

        await entity.async_added_to_hass()

        self.assertEqual(
            entity._attr_color_mode,
            light_module.ColorMode.COLOR_TEMP,
        )
        self.assertEqual(entity._attr_color_temp_kelvin, 6500)
        self.assertEqual(entity._attr_brightness, 64)

    async def test_rgb_fixture_can_restore_hs_mode(self) -> None:
        entity = _fixture(supports_hs=True)
        entity._test_last_state = SimpleNamespace(
            state="on",
            attributes={"hs_color": (120.0, 80.0)},
        )

        await entity.async_added_to_hass()

        self.assertEqual(entity._attr_color_mode, light_module.ColorMode.HS)
        self.assertEqual(entity._attr_hs_color, (120.0, 80.0))

    def test_availability_update_normalizes_stale_unsupported_mode(self) -> None:
        entity = _fixture(supports_hs=False)
        entity._attr_color_mode = light_module.ColorMode.HS

        entity._handle_availability(True)

        self.assertEqual(
            entity._attr_color_mode,
            light_module.ColorMode.COLOR_TEMP,
        )
        self.assertEqual(entity._write_count, 1)


if __name__ == "__main__":
    unittest.main()
