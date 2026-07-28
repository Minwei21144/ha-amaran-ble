"""Diagnostics support for Amaran BLE."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_APP_KEY, CONF_NET_KEY, DOMAIN


REDACTED = "**REDACTED**"
_SECRET_KEYS = {CONF_NET_KEY.lower(), CONF_APP_KEY.lower(), "netkey", "appkey"}
_MAC_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{12})"
    r"(?![0-9a-f])"
)


def _redact_secret_keys(value: Any) -> Any:
    """Recursively redact Mesh keys from a diagnostics payload."""
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and key.lower() in _SECRET_KEYS
            else _redact_secret_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact_secret_keys(item) for item in value]
    return value


def _redact_mac_text(text: str, aliases: dict[str, str]) -> str:
    """Replace every MAC address in text with a stable per-report alias."""

    def replace(match: re.Match[str]) -> str:
        normalized = match.group(0).replace(":", "").replace("-", "").upper()
        if normalized not in aliases:
            aliases[normalized] = f"**REDACTED_MAC_{len(aliases) + 1}**"
        return aliases[normalized]

    return _MAC_PATTERN.sub(replace, text)


def _redact_mac_addresses(value: Any, aliases: dict[str, str]) -> Any:
    """Recursively pseudonymize MAC addresses in values and dictionary keys."""
    if isinstance(value, dict):
        return {
            _redact_mac_text(key, aliases) if isinstance(key, str) else key:
            _redact_mac_addresses(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact_mac_addresses(item, aliases) for item in value]
    if isinstance(value, str):
        return _redact_mac_text(value, aliases)
    return value


def _redact_diagnostics_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Remove secrets and pseudonymize device addresses consistently."""
    without_secrets = _redact_secret_keys(data)
    return _redact_mac_addresses(without_secrets, {})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for one Amaran BLE config entry."""
    controller = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if controller is None:
        return _redact_diagnostics_payload(
            {
                "entry": dict(entry.data),
                "options": dict(entry.options),
                "loaded": False,
            }
        )

    controller_state = controller.diagnostic_state()
    return _redact_diagnostics_payload(
        {
            "entry": dict(entry.data),
            "options": dict(entry.options),
            "loaded": True,
            **controller_state,
        }
    )
