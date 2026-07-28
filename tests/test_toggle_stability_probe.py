from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import amaran_toggle_stability_probe as toggle_probe  # noqa: E402


def light_state(
    *,
    state: str = "on",
    modes: list[str] | None = None,
    color_mode: str | None = None,
    brightness: int = 128,
    kelvin: int | None = None,
    hs_color: list[float] | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict:
    return {
        "state": state,
        "attributes": {
            "supported_color_modes": modes or [],
            "color_mode": color_mode,
            "brightness": brightness,
            "color_temp_kelvin": kelvin,
            "hs_color": hs_color,
            "min_color_temp_kelvin": minimum,
            "max_color_temp_kelvin": maximum,
        },
    }


class ToggleStabilityProbeTests(unittest.TestCase):
    def test_token_can_be_read_from_environment_without_qr(self) -> None:
        with patch.dict(
            "os.environ",
            {"AMARAN_HA_TOKEN": "temporary-test-token"},
        ):
            token = toggle_probe.probe.resolve_token(
                None,
                "AMARAN_HA_TOKEN",
            )

        self.assertEqual(token, "temporary-test-token")

    def test_200x_sequence_skips_unsupported_hs(self) -> None:
        state = light_state(
            modes=["color_temp"],
            minimum=2700,
            maximum=6500,
        )

        sequence = toggle_probe.build_action_sequence(
            state,
            brightness_pct=1,
            brightness_toggle_pct=2,
            full_actions=False,
        )

        self.assertFalse(any("hs_color" in payload for _, payload in sequence))
        kelvin_values = [
            payload["color_temp_kelvin"]
            for _, payload in sequence
            if "color_temp_kelvin" in payload
        ]
        self.assertEqual(kelvin_values, [3200, 4500])

    def test_rgb_fixture_sequence_includes_hs(self) -> None:
        state = light_state(
            modes=["color_temp", "hs"],
            minimum=2500,
            maximum=7500,
        )

        sequence = toggle_probe.build_action_sequence(
            state,
            brightness_pct=1,
            brightness_toggle_pct=2,
            full_actions=False,
        )

        self.assertTrue(any("hs_color" in payload for _, payload in sequence))

    def test_kelvin_is_clamped_to_reported_fixture_range(self) -> None:
        capabilities = {"min_kelvin": 4000, "max_kelvin": 4200}

        self.assertEqual(toggle_probe.clamp_kelvin(3200, capabilities), 4000)
        self.assertEqual(toggle_probe.clamp_kelvin(4500, capabilities), 4200)

    def test_restore_action_preserves_color_temperature(self) -> None:
        state = light_state(
            color_mode="color_temp",
            brightness=77,
            kelvin=5600,
        )

        service, payload = toggle_probe.build_restore_action(state)

        self.assertEqual(service, "turn_on")
        self.assertEqual(
            payload,
            {"brightness": 77, "color_temp_kelvin": 5600},
        )

    def test_restore_action_preserves_hs(self) -> None:
        state = light_state(
            color_mode="hs",
            brightness=42,
            hs_color=[120.0, 90.0],
        )

        service, payload = toggle_probe.build_restore_action(state)

        self.assertEqual(service, "turn_on")
        self.assertEqual(
            payload,
            {"brightness": 42, "hs_color": [120.0, 90.0]},
        )

    def test_restore_action_keeps_off_light_off(self) -> None:
        service, payload = toggle_probe.build_restore_action(
            light_state(state="off")
        )

        self.assertEqual(service, "turn_off")
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
