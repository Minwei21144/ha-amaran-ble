"""Offline checks for Amaran config parsing."""

from __future__ import annotations

import json
import unittest

from custom_components.amaran_ble.models import parse_config_json


class ConfigParsingTest(unittest.TestCase):
    def test_parse_import_json(self) -> None:
        config = parse_config_json(
            json.dumps(
                {
                    "net_key": "00112233445566778899AABBCCDDEEFF",
                    "app_key": "FFEEDDCCBBAA99887766554433221100",
                    "relay_hub": "AABBCC112233",
                    "lights": [
                        {
                            "name": "amaran T2c #1",
                            "mac": "AA:BB:CC:11:22:33",
                            "mesh_address": "0x0005",
                            "min_kelvin": 2500,
                            "max_kelvin": 7500,
                        }
                    ],
                }
            )
        )
        self.assertEqual(config.relay_hub, "AA:BB:CC:11:22:33")
        self.assertEqual(config.lights[0].mesh_address, 5)

    def test_parse_import_json_without_relay_hub(self) -> None:
        config = parse_config_json(
            json.dumps(
                {
                    "net_key": "00112233445566778899AABBCCDDEEFF",
                    "app_key": "FFEEDDCCBBAA99887766554433221100",
                    "lights": [
                        {
                            "name": "amaran T2c #1",
                            "mac": "AA:BB:CC:11:22:33",
                            "mesh_address": 5,
                        }
                    ],
                }
            )
        )
        self.assertEqual(config.relay_hub, "AA:BB:CC:11:22:33")
        self.assertEqual(config.lights[0].mac, "AA:BB:CC:11:22:33")

    def test_parse_proxy_candidates(self) -> None:
        config = parse_config_json(
            json.dumps(
                {
                    "net_key": "00112233445566778899AABBCCDDEEFF",
                    "app_key": "FFEEDDCCBBAA99887766554433221100",
                    "lights": [
                        {
                            "name": "amaran T2c #1",
                            "mac": "AA:BB:CC:11:22:33",
                            "mesh_address": 5,
                        }
                    ],
                    "proxy_candidates": [
                        "AABBCC445566",
                        "AA:BB:CC:11:22:33",
                        "AA:BB:CC:44:55:66",
                    ],
                }
            )
        )
        self.assertEqual(
            config.proxy_candidates,
            ["AA:BB:CC:44:55:66", "AA:BB:CC:11:22:33"],
        )

    def test_parse_bicolor_fixture_capabilities(self) -> None:
        config = parse_config_json(
            json.dumps(
                {
                    "net_key": "00112233445566778899AABBCCDDEEFF",
                    "app_key": "FFEEDDCCBBAA99887766554433221100",
                    "lights": [
                        {
                            "name": "amaran 200x #1",
                            "mac": "AA:BB:CC:11:22:33",
                            "mesh_address": 6,
                            "min_kelvin": 2700,
                            "max_kelvin": 6500,
                            "supports_hs": False,
                            "supports_gm": False,
                        }
                    ],
                }
            )
        )
        light = config.lights[0]
        self.assertEqual(light.min_kelvin, 2700)
        self.assertEqual(light.max_kelvin, 6500)
        self.assertFalse(light.supports_hs)
        self.assertFalse(light.supports_gm)

    def test_reject_bad_key(self) -> None:
        with self.assertRaises(ValueError):
            parse_config_json(
                json.dumps(
                    {
                        "net_key": "bad",
                        "app_key": "FFEEDDCCBBAA99887766554433221100",
                        "relay_hub": "AA:BB:CC:11:22:33",
                        "lights": [{"mac": "AA:BB:CC:11:22:33", "mesh_address": 5}],
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
