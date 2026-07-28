"""Offline checks for Amaran Telink payload encoding."""

from __future__ import annotations

import unittest

from custom_components.amaran_ble import telink


def hx(payload: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in payload)


class TelinkPayloadTest(unittest.TestCase):
    def test_payloads_from_pymeshsdk_capture(self) -> None:
        cases = {
            "off": (telink.onoff(False), "8C 00 00 00 00 00 00 00 00 8C"),
            "on": (telink.onoff(True), "8D 00 00 00 00 00 00 00 01 8C"),
            "brightness_50": (
                telink.brightness(500),
                "0C 00 00 00 00 00 00 00 7D 8F",
            ),
            "brightness_100": (
                telink.brightness(1000),
                "89 00 00 00 00 00 00 00 FA 8F",
            ),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(hx(payload), expected)
                self.assertEqual(payload[0], telink.checksum(payload))

    def test_reference_cct_hsi_payloads(self) -> None:
        cases = {
            "cct_2500": (
                telink.cct(2500, 500, 0),
                "EF 00 00 00 00 40 A1 0F 7D 82",
            ),
            "cct_4500": (
                telink.cct(4500, 500, 0),
                "7C 00 00 00 00 40 21 1C 7D 82",
            ),
            "cct_7500": (
                telink.cct(7500, 500, 0),
                "4E 00 00 00 00 40 E1 2E 7D 82",
            ),
            "hsi_red": (
                telink.hsi(0, 100, 500),
                "17 00 00 00 00 00 19 00 7D 81",
            ),
            "hsi_green": (
                telink.hsi(120, 100, 500),
                "26 00 00 00 00 00 19 0F 7D 81",
            ),
            "hsi_blue": (
                telink.hsi(240, 100, 500),
                "35 00 00 00 00 00 19 1E 7D 81",
            ),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(hx(payload), expected)
                self.assertEqual(payload[0], telink.checksum(payload))


if __name__ == "__main__":
    unittest.main()
