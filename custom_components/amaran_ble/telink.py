"""Telink opcode 0x26 payload helpers for Amaran fixtures."""

from __future__ import annotations

from dataclasses import dataclass

PAYLOAD_LEN = 10


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


def checksum(payload: bytes | bytearray) -> int:
    """Return the Telink checksum byte."""
    if len(payload) < PAYLOAD_LEN:
        raise ValueError("Telink payload must be at least 10 bytes")
    return sum(payload[1:PAYLOAD_LEN]) & 0xFF


def finalize(payload: bytearray) -> bytes:
    """Set the checksum byte and return an immutable payload."""
    if len(payload) != PAYLOAD_LEN:
        raise ValueError("Telink payload must be exactly 10 bytes")
    payload[0] = checksum(payload)
    return bytes(payload)


def status_request() -> bytes:
    """Build a status request payload."""
    payload = bytearray(PAYLOAD_LEN)
    payload[9] = 0x0E
    return finalize(payload)


def onoff(on: bool) -> bytes:
    """Build an on/off payload."""
    payload = bytearray(PAYLOAD_LEN)
    payload[8] = 0x01 if on else 0x00
    payload[9] = 0x8C
    return finalize(payload)


def brightness(intensity: int) -> bytes:
    """Build a brightness payload using Amaran's 0..1000 scale."""
    value = _clamp(intensity, 0, 1000)
    payload = bytearray(PAYLOAD_LEN)
    payload[7] = (value & 0x03) << 6
    payload[8] = (value >> 2) & 0xFF
    payload[9] = 0x8F
    return finalize(payload)


def cct(kelvin: int, intensity: int, gm_offset: int = 0) -> bytes:
    """Build a CCT payload.

    ``kelvin`` is the fixture color temperature in K.
    ``intensity`` is 0..1000.
    ``gm_offset`` is -10..+10, where 0 is neutral.
    """
    value = _clamp(intensity, 0, 1000)
    telink_cct = (int(kelvin) + 5) // 10
    telink_cct = _clamp(telink_cct, 80, 2000)
    gm = _clamp(gm_offset, -10, 10) + 10
    gm_flag = 0

    low64 = (value & 0x03) << 62
    high16 = 0x8200 | ((value >> 2) & 0xFF)

    if telink_cct < 1001:
        low64 |= telink_cct << 52
        high16 |= (telink_cct >> 12) & 0xFF
    else:
        low64 |= ((telink_cct + 0x18) & 0x3FF) << 52
        low64 |= 0x0000040000000000

    low64 |= (gm_flag & 0x01) << 43
    low64 |= (gm & 0x7F) << 45

    payload = bytearray(PAYLOAD_LEN)
    for index in range(8):
        payload[index] = (low64 >> (index * 8)) & 0xFF
    payload[8] = high16 & 0xFF
    payload[9] = (high16 >> 8) & 0xFF
    return finalize(payload)


def hsi(hue: int, saturation: int, intensity: int) -> bytes:
    """Build an HSI payload.

    ``hue`` is 0..360 degrees, ``saturation`` is 0..100, and ``intensity`` is
    0..1000.
    """
    value = _clamp(intensity, 0, 1000)
    hue_value = _clamp(hue, 0, 360) & 0x1FF
    sat_value = _clamp(saturation, 0, 100) & 0x7F

    payload = bytearray(PAYLOAD_LEN)
    payload[5] = (sat_value & 0x03) << 6
    payload[6] = ((hue_value & 0x07) << 5) | ((sat_value >> 2) & 0x1F)
    payload[7] = ((hue_value >> 3) & 0x3F) | ((value & 0x03) << 6)
    payload[8] = (value >> 2) & 0xFF
    payload[9] = 0x81
    return finalize(payload)


@dataclass(frozen=True)
class TelinkStatus:
    """Decoded Telink status payload."""

    on: bool
    is_hs: bool
    intensity: int
    cct_kelvin: int
    gm: int
    hue: int
    saturation: int
    command_type: int


def decode_status(payload: bytes) -> TelinkStatus | None:
    """Decode a 10-byte Telink status payload."""
    if len(payload) < PAYLOAD_LEN or payload[0] != checksum(payload):
        return None

    command_type = payload[9] & 0x7F
    low64 = 0
    for index in range(8):
        low64 |= payload[index] << (index * 8)
    high16 = payload[8] | (payload[9] << 8)
    on = ((low64 >> 8) & 0x01) == 1

    if command_type == 0x02:
        cct_raw = (low64 >> 52) & 0x3FF
        cct_flag = (low64 >> 42) & 0x01
        telink_cct = cct_raw + 1000 if cct_flag else cct_raw
        intensity = ((high16 << 2) | ((low64 >> 62) & 0x03)) & 0x3FF
        gm = ((low64 >> 45) & 0x7F) - 10
        gm = _clamp(gm, -10, 10)
        return TelinkStatus(
            on=on,
            is_hs=False,
            intensity=intensity,
            cct_kelvin=telink_cct * 10,
            gm=gm,
            hue=0,
            saturation=0,
            command_type=command_type,
        )

    if command_type == 0x01:
        saturation = ((payload[6] & 0x1F) << 2) | ((payload[5] >> 6) & 0x03)
        hue = ((payload[7] & 0x3F) << 3) | ((payload[6] >> 5) & 0x07)
        intensity = (payload[8] << 2) | ((payload[7] >> 6) & 0x03)
        return TelinkStatus(
            on=on,
            is_hs=True,
            intensity=_clamp(intensity, 0, 1000),
            cct_kelvin=0,
            gm=0,
            hue=_clamp(hue, 0, 360),
            saturation=_clamp(saturation, 0, 100),
            command_type=command_type,
        )

    return None
