"""Offline checks for precise Amaran color number entities."""

from __future__ import annotations

from enum import Enum
import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest


def _install_homeassistant_number_stubs() -> None:
    """Install the small HA number surface needed by these tests."""
    homeassistant = sys.modules.setdefault(
        "homeassistant",
        ModuleType("homeassistant"),
    )
    components = sys.modules.setdefault(
        "homeassistant.components",
        ModuleType("homeassistant.components"),
    )
    helpers = sys.modules.setdefault(
        "homeassistant.helpers",
        ModuleType("homeassistant.helpers"),
    )

    number = ModuleType("homeassistant.components.number")

    class NumberMode(str, Enum):
        SLIDER = "slider"

    class NumberEntity:
        @property
        def unique_id(self) -> str | None:
            return getattr(self, "_attr_unique_id", None)

        def async_on_remove(self, callback: object) -> None:
            self._remove_callbacks = getattr(self, "_remove_callbacks", [])
            self._remove_callbacks.append(callback)

        def async_write_ha_state(self) -> None:
            self._write_count = getattr(self, "_write_count", 0) + 1

    number.DOMAIN = "number"
    number.NumberEntity = NumberEntity
    number.NumberMode = NumberMode

    entity_registry = ModuleType("homeassistant.helpers.entity_registry")

    class FakeEntityRegistry:
        def __init__(self) -> None:
            self.entries: list[SimpleNamespace] = []
            self.removed: list[str] = []

        def async_remove(self, entity_id: str) -> None:
            self.removed.append(entity_id)
            self.entries = [
                entry for entry in self.entries if entry.entity_id != entity_id
            ]

    def async_get(hass: object) -> FakeEntityRegistry:
        registry = getattr(hass, "entity_registry", None)
        if registry is None:
            registry = FakeEntityRegistry()
            hass.entity_registry = registry
        return registry

    def async_entries_for_config_entry(
        registry: FakeEntityRegistry,
        config_entry_id: str,
    ) -> list[SimpleNamespace]:
        return [
            entry
            for entry in registry.entries
            if entry.config_entry_id == config_entry_id
        ]

    entity_registry.FakeEntityRegistry = FakeEntityRegistry
    entity_registry.async_get = async_get
    entity_registry.async_entries_for_config_entry = (
        async_entries_for_config_entry
    )

    config_entries = sys.modules.setdefault(
        "homeassistant.config_entries",
        ModuleType("homeassistant.config_entries"),
    )
    config_entries.ConfigEntry = getattr(
        config_entries,
        "ConfigEntry",
        object,
    )

    core = sys.modules.setdefault(
        "homeassistant.core",
        ModuleType("homeassistant.core"),
    )
    core.HomeAssistant = getattr(core, "HomeAssistant", object)

    entity_platform = sys.modules.setdefault(
        "homeassistant.helpers.entity_platform",
        ModuleType("homeassistant.helpers.entity_platform"),
    )
    entity_platform.AddEntitiesCallback = getattr(
        entity_platform,
        "AddEntitiesCallback",
        object,
    )

    const = sys.modules.setdefault(
        "homeassistant.const",
        ModuleType("homeassistant.const"),
    )
    if not hasattr(const, "Platform"):
        class Platform(str, Enum):
            LIGHT = "light"
            NUMBER = "number"
            SWITCH = "switch"
            SENSOR = "sensor"

        const.Platform = Platform

    components.number = number
    helpers.entity_platform = entity_platform
    helpers.entity_registry = entity_registry
    homeassistant.components = components
    homeassistant.helpers = helpers
    sys.modules["homeassistant.components.number"] = number
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry


_install_homeassistant_number_stubs()
created_controller_stub = False
if "custom_components.amaran_ble.controller" not in sys.modules:
    controller_stub = ModuleType("custom_components.amaran_ble.controller")
    controller_stub.AmaranBleController = object
    sys.modules["custom_components.amaran_ble.controller"] = controller_stub
    created_controller_stub = True
number_module = importlib.import_module("custom_components.amaran_ble.number")
if created_controller_stub:
    del sys.modules["custom_components.amaran_ble.controller"]
color_state_module = importlib.import_module(
    "custom_components.amaran_ble.color_state"
)
models_module = importlib.import_module("custom_components.amaran_ble.models")
const_module = importlib.import_module("custom_components.amaran_ble.const")


class _FakeController:
    def __init__(self, lights: list[object]) -> None:
        self.config = models_module.AmaranConfig(
            net_key="00112233445566778899AABBCCDDEEFF",
            app_key="FFEEDDCCBBAA99887766554433221100",
            relay_hub=lights[0].mac,
            lights=lights,
            proxy_candidates=[],
        )
        self.states = {
            light.mesh_address:
                color_state_module.FixtureColorState.from_config(light)
            for light in lights
        }
        self.calls: list[tuple[object, ...]] = []

    def is_light_available(self, _mac: str) -> bool:
        return True

    def async_track_availability(
        self,
        _mac: str,
        _callback: object,
    ) -> object:
        return lambda: None

    def async_track_fixture_state(
        self,
        _mesh_address: int,
        _callback: object,
    ) -> object:
        return lambda: None

    def fixture_state(self, mesh_address: int) -> object:
        return self.states[mesh_address].snapshot()

    async def async_turn_on(
        self,
        mac: str,
        mesh_address: int,
    ) -> None:
        self.calls.append(("turn_on", mac, mesh_address))
        self.states[mesh_address].apply_power(True)

    async def async_set_cct(
        self,
        mac: str,
        mesh_address: int,
        kelvin: int,
        intensity: int,
        gm: int,
    ) -> None:
        self.calls.append(
            ("set_cct", mac, mesh_address, kelvin, intensity, gm)
        )
        self.states[mesh_address].apply_cct(
            kelvin,
            intensity,
            gm,
        )

    async def async_set_brightness(
        self,
        mac: str,
        mesh_address: int,
        intensity: int,
    ) -> None:
        self.calls.append(
            ("set_brightness", mac, mesh_address, intensity)
        )
        self.states[mesh_address].apply_intensity(intensity)

    async def async_set_hsi(
        self,
        mac: str,
        mesh_address: int,
        hue: int,
        saturation: int,
        intensity: int,
    ) -> None:
        self.calls.append(
            (
                "set_hsi",
                mac,
                mesh_address,
                hue,
                saturation,
                intensity,
            )
        )
        self.states[mesh_address].apply_hsi(
            hue,
            saturation,
            intensity,
        )


def _fixed_white_fixture() -> object:
    return models_module.AmaranLightConfig(
        name="amaran fixed white",
        mac="AA:BB:CC:77:88:99",
        mesh_address=9,
        min_kelvin=5600,
        max_kelvin=5600,
        supports_hs=False,
        supports_gm=False,
    )


def _fixtures() -> tuple[object, object]:
    rgb = models_module.AmaranLightConfig(
        name="amaran T2c",
        mac="AA:BB:CC:11:22:33",
        mesh_address=3,
        min_kelvin=2500,
        max_kelvin=7500,
        supports_hs=True,
        supports_gm=True,
    )
    bicolor = models_module.AmaranLightConfig(
        name="amaran 200x",
        mac="AA:BB:CC:44:55:66",
        mesh_address=6,
        min_kelvin=2700,
        max_kelvin=6500,
        supports_hs=False,
        supports_gm=False,
    )
    return rgb, bicolor


class NumberEntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_only_exposes_supported_fixture_controls(
        self,
    ) -> None:
        rgb, bicolor = _fixtures()
        controller = _FakeController([rgb, bicolor])
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(
            data={const_module.DOMAIN: {"test": controller}}
        )
        entities: list[object] = []

        await number_module.async_setup_entry(
            hass,
            entry,
            entities.extend,
        )

        rgb_controls = {
            entity._control
            for entity in entities
            if entity._config.mesh_address == rgb.mesh_address
        }
        bicolor_controls = {
            entity._control
            for entity in entities
            if entity._config.mesh_address == bicolor.mesh_address
        }
        rgb_control_order = [
            entity._control
            for entity in entities
            if entity._config.mesh_address == rgb.mesh_address
        ]
        bicolor_control_order = [
            entity._control
            for entity in entities
            if entity._config.mesh_address == bicolor.mesh_address
        ]
        self.assertEqual(
            rgb_controls,
            set(number_module.AdvancedControl),
        )
        self.assertEqual(
            bicolor_controls,
            {
                number_module.AdvancedControl.BRIGHTNESS,
                number_module.AdvancedControl.COLOR_TEMPERATURE,
            },
        )
        self.assertEqual(
            rgb_control_order,
            [
                number_module.AdvancedControl.BRIGHTNESS,
                number_module.AdvancedControl.COLOR_TEMPERATURE,
                number_module.AdvancedControl.GREEN_MAGENTA,
                number_module.AdvancedControl.HUE,
                number_module.AdvancedControl.SATURATION,
            ],
        )
        self.assertEqual(
            bicolor_control_order,
            [
                number_module.AdvancedControl.BRIGHTNESS,
                number_module.AdvancedControl.COLOR_TEMPERATURE,
            ],
        )

    async def test_fixed_white_fixture_only_exposes_brightness(self) -> None:
        fixed_white = _fixed_white_fixture()
        controller = _FakeController([fixed_white])
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(
            data={const_module.DOMAIN: {"test": controller}}
        )
        entities: list[object] = []

        await number_module.async_setup_entry(
            hass,
            entry,
            entities.extend,
        )

        self.assertEqual(
            [entity._control for entity in entities],
            [number_module.AdvancedControl.BRIGHTNESS],
        )
        self.assertEqual(
            number_module.supported_controls(fixed_white),
            [number_module.AdvancedControl.BRIGHTNESS],
        )

    async def test_setup_removes_stale_color_temperature_control(self) -> None:
        fixed_white = _fixed_white_fixture()
        rgb, _bicolor = _fixtures()
        controller = _FakeController([rgb, fixed_white])
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(
            data={const_module.DOMAIN: {"test": controller}}
        )
        registry_module = sys.modules["homeassistant.helpers.entity_registry"]
        registry = registry_module.FakeEntityRegistry()
        hass.entity_registry = registry
        stale_unique_id = (
            "amaran_ble_aabbcc778899_0009_"
            f"{number_module.AdvancedControl.COLOR_TEMPERATURE.value}"
        )
        kept_unique_id = (
            "amaran_ble_aabbcc778899_0009_"
            f"{number_module.AdvancedControl.BRIGHTNESS.value}"
        )
        registry.entries = [
            SimpleNamespace(
                entity_id="number.fixed_white_color_temperature",
                unique_id=stale_unique_id,
                domain="number",
                config_entry_id="test",
            ),
            SimpleNamespace(
                entity_id="number.fixed_white_brightness",
                unique_id=kept_unique_id,
                domain="number",
                config_entry_id="test",
            ),
            SimpleNamespace(
                entity_id="light.fixed_white",
                unique_id="amaran_ble_aabbcc778899_0009",
                domain="light",
                config_entry_id="test",
            ),
            SimpleNamespace(
                entity_id="number.other_integration",
                unique_id="unrelated",
                domain="number",
                config_entry_id="other",
            ),
        ]
        entities: list[object] = []

        await number_module.async_setup_entry(
            hass,
            entry,
            entities.extend,
        )

        self.assertEqual(
            registry.removed,
            ["number.fixed_white_color_temperature"],
        )
        self.assertIn(kept_unique_id, {entity.unique_id for entity in entities})

    async def test_cct_control_uses_100_kelvin_steps(self) -> None:
        rgb, _bicolor = _fixtures()
        controller = _FakeController([rgb])
        entity = number_module.AmaranBleColorNumber(
            controller,
            rgb,
            number_module.AdvancedControl.COLOR_TEMPERATURE,
        )

        await entity.async_set_native_value(4550)

        self.assertEqual(entity._attr_native_step, 100)
        self.assertEqual(
            controller.calls,
            [
                ("turn_on", rgb.mac, rgb.mesh_address),
                (
                    "set_cct",
                    rgb.mac,
                    rgb.mesh_address,
                    4600,
                    1000,
                    0,
                ),
            ],
        )
        self.assertEqual(entity.native_value, 4600)

    async def test_gm_control_preserves_cct_and_intensity(self) -> None:
        rgb, _bicolor = _fixtures()
        controller = _FakeController([rgb])
        controller.states[rgb.mesh_address].apply_cct(5600, 473, 2)
        entity = number_module.AmaranBleColorNumber(
            controller,
            rgb,
            number_module.AdvancedControl.GREEN_MAGENTA,
        )

        await entity.async_set_native_value(-4)

        self.assertEqual(
            controller.calls[-1],
            (
                "set_cct",
                rgb.mac,
                rgb.mesh_address,
                5600,
                473,
                -4,
            ),
        )
        self.assertEqual(entity.native_value, -4)

    async def test_hsi_controls_and_brightness_preserve_state(self) -> None:
        rgb, _bicolor = _fixtures()
        controller = _FakeController([rgb])
        controller.states[rgb.mesh_address].apply_hsi(200, 45, 500)
        hue = number_module.AmaranBleColorNumber(
            controller,
            rgb,
            number_module.AdvancedControl.HUE,
        )
        saturation = number_module.AmaranBleColorNumber(
            controller,
            rgb,
            number_module.AdvancedControl.SATURATION,
        )
        brightness = number_module.AmaranBleColorNumber(
            controller,
            rgb,
            number_module.AdvancedControl.BRIGHTNESS,
        )

        await hue.async_set_native_value(241)
        await saturation.async_set_native_value(82)
        await brightness.async_set_native_value(47.3)

        self.assertEqual(
            controller.calls[-5::2],
            [
                (
                    "set_hsi",
                    rgb.mac,
                    rgb.mesh_address,
                    241,
                    45,
                    500,
                ),
                (
                    "set_hsi",
                    rgb.mac,
                    rgb.mesh_address,
                    241,
                    82,
                    500,
                ),
                (
                    "set_brightness",
                    rgb.mac,
                    rgb.mesh_address,
                    473,
                ),
            ],
        )
        self.assertEqual(hue.native_value, 241)
        self.assertEqual(saturation.native_value, 82)
        self.assertEqual(brightness.native_value, 47.3)

    def test_unchanged_channel_does_not_write_ha_state(self) -> None:
        rgb, _bicolor = _fixtures()
        controller = _FakeController([rgb])
        entity = number_module.AmaranBleColorNumber(
            controller,
            rgb,
            number_module.AdvancedControl.HUE,
        )
        unchanged = controller.fixture_state(rgb.mesh_address)
        changed = unchanged.snapshot()
        changed.apply_hsi(120, 100, 1000)

        entity._handle_fixture_state(unchanged)
        entity._handle_fixture_state(changed)
        entity._handle_fixture_state(changed)

        self.assertEqual(entity.native_value, 120)
        self.assertEqual(entity._write_count, 1)


if __name__ == "__main__":
    unittest.main()
