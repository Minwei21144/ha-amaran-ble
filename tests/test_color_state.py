"""Offline checks for shared Amaran fixture color state."""

from __future__ import annotations

import unittest

from custom_components.amaran_ble.color_state import (
    FixtureColorState,
    quantize_kelvin,
)
from custom_components.amaran_ble.models import AmaranLightConfig
from custom_components.amaran_ble.telink import TelinkStatus


def _config(
    *,
    supports_hs: bool = True,
    supports_gm: bool = True,
) -> AmaranLightConfig:
    return AmaranLightConfig(
        name="Test fixture",
        mac="AA:BB:CC:11:22:33",
        mesh_address=3,
        min_kelvin=2700,
        max_kelvin=6500,
        supports_hs=supports_hs,
        supports_gm=supports_gm,
    )


class ColorStateTests(unittest.TestCase):
    def test_kelvin_rounds_to_nearest_100_and_clamps(self) -> None:
        self.assertEqual(quantize_kelvin(4549, 2700, 6500), 4500)
        self.assertEqual(quantize_kelvin(4550, 2700, 6500), 4600)
        self.assertEqual(quantize_kelvin(2600, 2700, 6500), 2700)
        self.assertEqual(quantize_kelvin(7000, 2700, 6500), 6500)

    def test_bicolor_fixture_rejects_hsi_and_neutralizes_gm(self) -> None:
        state = FixtureColorState.from_config(
            _config(supports_hs=False, supports_gm=False)
        )

        state.apply_cct(4567, 473, 9)

        self.assertEqual(state.kelvin, 4600)
        self.assertEqual(state.intensity, 473)
        self.assertEqual(state.gm, 0)
        with self.assertRaisesRegex(ValueError, "does not support HSI"):
            state.apply_hsi(120, 80, 500)

    def test_status_updates_every_color_channel(self) -> None:
        state = FixtureColorState.from_config(_config())
        status = TelinkStatus(
            on=True,
            is_hs=True,
            intensity=473,
            cct_kelvin=0,
            gm=0,
            hue=241,
            saturation=82,
            command_type=1,
        )

        state.apply_status(status)

        self.assertTrue(state.on)
        self.assertTrue(state.is_hs)
        self.assertEqual(state.intensity, 473)
        self.assertEqual(state.hue, 241)
        self.assertEqual(state.saturation, 82)

    def test_snapshot_is_detached(self) -> None:
        state = FixtureColorState.from_config(_config())
        snapshot = state.snapshot()

        snapshot.apply_cct(5600, 500, -3)

        self.assertEqual(state.kelvin, 4500)
        self.assertEqual(state.gm, 0)
        self.assertEqual(snapshot.kelvin, 5600)
        self.assertEqual(snapshot.gm, -3)


if __name__ == "__main__":
    unittest.main()
