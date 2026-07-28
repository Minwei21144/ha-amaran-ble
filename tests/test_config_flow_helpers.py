"""Offline checks for config flow helper behavior."""

from __future__ import annotations

import unittest

from custom_components.amaran_ble.config_flow import AmaranBleConfigFlow
from custom_components.amaran_ble.models import parse_config_json


class ConfigFlowHelperTest(unittest.TestCase):
    def test_discovery_title_includes_mac_suffix(self) -> None:
        flow = AmaranBleConfigFlow()
        self.assertEqual(
            flow._discovery_title("Mesh Device", "AA:BB:CC:11:22:33"),
            "Mesh Device 11:22:33",
        )

    def test_discovery_title_avoids_duplicate_suffix(self) -> None:
        flow = AmaranBleConfigFlow()
        self.assertEqual(
            flow._discovery_title("Mesh Device 11:22:33", "AA:BB:CC:11:22:33"),
            "Mesh Device 11:22:33",
        )

    def test_discovered_address_is_configured(self) -> None:
        flow = AmaranBleConfigFlow()
        self.assertTrue(
            flow._discovered_address_is_configured(
                "AA:BB:CC:11:22:33",
                [
                    {
                        "relay_hub": "AA:BB:CC:44:55:66",
                        "lights": [
                            {
                                "mac": "AA:BB:CC:11:22:33",
                                "mesh_address": 5,
                            }
                        ],
                    }
                ],
            )
        )

    def test_discovered_address_suffix_match_is_configured(self) -> None:
        flow = AmaranBleConfigFlow()
        self.assertTrue(
            flow._discovered_address_is_configured(
                "00:11:22:44:55:66",
                [{"proxy_candidates": ["AA:BB:CC:44:55:66"]}],
            )
        )

    def test_entry_known_mac_sources_includes_last_successful_proxy(self) -> None:
        flow = AmaranBleConfigFlow()
        entry = type(
            "Entry",
            (),
            {
                "data": {
                    "lights": [
                        {
                            "mac": "AA:BB:CC:11:22:33",
                            "mesh_address": 5,
                        }
                    ]
                },
                "options": {"last_successful_proxy": "AA:BB:CC:44:55:66"},
            },
        )()

        self.assertTrue(
            flow._discovered_address_is_configured(
                "AA:BB:CC:44:55:66",
                [flow._entry_known_mac_sources(entry)],
            )
        )

    def test_manual_import_aborts_matching_bluetooth_discovery_flows(self) -> None:
        class FakeFlowManager:
            def __init__(self) -> None:
                self.aborted: list[str] = []

            def async_progress_by_handler(self, handler: str) -> list[dict[str, object]]:
                self.handler = handler
                return [
                    {
                        "flow_id": "current",
                        "context": {
                            "source": "user",
                            "unique_id": "02:00:00:00:00:01",
                        },
                    },
                    {
                        "flow_id": "matching-light",
                        "context": {
                            "source": "bluetooth",
                            "unique_id": "02:00:00:00:00:02",
                        },
                    },
                    {
                        "flow_id": "matching-suffix",
                        "context": {
                            "source": "bluetooth",
                            "unique_id": "00:11:22:00:00:04",
                        },
                    },
                    {
                        "flow_id": "different-device",
                        "context": {
                            "source": "bluetooth",
                            "unique_id": "AA:BB:CC:11:22:33",
                        },
                    },
                    {
                        "flow_id": "different-source",
                        "context": {
                            "source": "user",
                            "unique_id": "02:00:00:00:00:02",
                        },
                    },
                ]

            def async_abort(self, flow_id: str) -> None:
                self.aborted.append(flow_id)

        manager = FakeFlowManager()
        flow = AmaranBleConfigFlow()
        flow.flow_id = "current"
        flow.hass = type(
            "Hass",
            (),
            {
                "config_entries": type(
                    "ConfigEntries",
                    (),
                    {"flow": manager},
                )()
            },
        )()
        config = parse_config_json(
            """
            {
              "net_key": "00112233445566778899AABBCCDDEEFF",
              "app_key": "FFEEDDCCBBAA99887766554433221100",
              "relay_hub": "02:00:00:00:00:01",
              "lights": [
                {
                  "name": "amaran T2c #1",
                  "mac": "02:00:00:00:00:02",
                  "mesh_address": 5
                },
                {
                  "name": "amaran 300c #1",
                  "mac": "02:00:00:00:00:04",
                  "mesh_address": 7
                }
              ]
            }
            """
        )

        flow._abort_matching_discovery_flows(config)

        self.assertEqual(manager.handler, "amaran_ble")
        self.assertEqual(
            manager.aborted,
            ["matching-light", "matching-suffix"],
        )


if __name__ == "__main__":
    unittest.main()
