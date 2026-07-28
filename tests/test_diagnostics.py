"""Offline checks for diagnostics privacy."""

from __future__ import annotations

import json
import sys
from types import ModuleType
import unittest


try:
    import homeassistant.config_entries  # type: ignore[import-not-found]
    import homeassistant.core  # type: ignore[import-not-found]
except ModuleNotFoundError:
    homeassistant = ModuleType("homeassistant")
    config_entries = ModuleType("homeassistant.config_entries")
    core = ModuleType("homeassistant.core")
    config_entries.ConfigEntry = object
    core.HomeAssistant = object
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.core"] = core

from custom_components.amaran_ble.diagnostics import (  # noqa: E402
    REDACTED,
    _redact_diagnostics_payload,
)


class DiagnosticsPrivacyTest(unittest.TestCase):
    def test_mesh_keys_and_mac_addresses_are_redacted_recursively(self) -> None:
        first_mac = "02:00:00:00:00:01"
        second_mac = "02-00-00-00-00-02"
        payload = {
            "entry": {
                "net_key": "00112233445566778899AABBCCDDEEFF",
                "app_key": "FFEEDDCCBBAA99887766554433221100",
                "relay_hub": first_mac,
                "lights": [{"mac": first_mac, "name": "Studio key"}],
            },
            "active_proxy": first_mac,
            "proxy_candidate_details": {
                first_mac: {"error": f"Failed to connect to {first_mac}"},
                second_mac: {"visible": True},
            },
            "nested": [{"netKey": "secret"}, {"appKey": "secret"}],
        }

        redacted = _redact_diagnostics_payload(payload)
        serialized = json.dumps(redacted, sort_keys=True)

        self.assertNotIn(first_mac, serialized)
        self.assertNotIn(second_mac, serialized)
        self.assertNotIn("00112233445566778899AABBCCDDEEFF", serialized)
        self.assertNotIn("FFEEDDCCBBAA99887766554433221100", serialized)
        self.assertEqual(redacted["entry"]["net_key"], REDACTED)
        self.assertEqual(redacted["entry"]["app_key"], REDACTED)
        self.assertEqual(redacted["nested"][0]["netKey"], REDACTED)
        self.assertEqual(redacted["nested"][1]["appKey"], REDACTED)
        self.assertEqual(
            redacted["entry"]["relay_hub"],
            redacted["active_proxy"],
        )
        self.assertIn(
            redacted["active_proxy"],
            redacted["proxy_candidate_details"],
        )
        self.assertIn("**REDACTED_MAC_2**", serialized)
        self.assertEqual(redacted["entry"]["lights"][0]["name"], "Studio key")


if __name__ == "__main__":
    unittest.main()
