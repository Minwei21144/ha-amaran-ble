"""Data models and config parsing for Amaran BLE."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .const import (
    CONF_APP_KEY,
    CONF_LIGHTS,
    CONF_MAC,
    CONF_MESH_ADDRESS,
    CONF_NET_KEY,
    CONF_PROXY_CANDIDATES,
    CONF_RELAY_HUB,
)

HEX_128_RE = re.compile(r"^[0-9a-fA-F]{32}$")
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([-:][0-9a-fA-F]{2}){5}$")


@dataclass(frozen=True)
class AmaranLightConfig:
    """One Amaran fixture in the mesh."""

    name: str
    mac: str
    mesh_address: int
    min_kelvin: int = 2500
    max_kelvin: int = 7500
    supports_hs: bool = True
    supports_gm: bool = True

    @property
    def supports_cct(self) -> bool:
        """Return whether the fixture has an adjustable color temperature.

        Fixed-white fixtures are exported with ``min_kelvin == max_kelvin``.
        They accept power and brightness commands only, so no color
        temperature control should be offered for them.
        """
        return self.max_kelvin > self.min_kelvin


@dataclass(frozen=True)
class AmaranConfig:
    """Stored Amaran mesh config."""

    net_key: str
    app_key: str
    relay_hub: str | None
    lights: list[AmaranLightConfig]
    proxy_candidates: list[str]


def normalize_mac(mac: str) -> str:
    """Return a canonical uppercase colon-separated MAC address."""
    raw = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(raw) != 12:
        raise ValueError("MAC address must contain 12 hex digits")
    return ":".join(raw[index : index + 2] for index in range(0, 12, 2)).upper()


def parse_config_json(text: str) -> AmaranConfig:
    """Parse the integration import JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON: {err.msg}") from err

    if not isinstance(data, dict):
        raise ValueError("Config JSON must be an object")

    net_key = str(data.get(CONF_NET_KEY) or data.get("netKey") or "").strip().upper()
    app_key = str(data.get(CONF_APP_KEY) or data.get("appKey") or "").strip().upper()
    raw_relay_hub = str(data.get(CONF_RELAY_HUB) or data.get("relayHub") or "").strip()
    raw_proxy_candidates = data.get(
        CONF_PROXY_CANDIDATES,
        data.get("proxyCandidates", []),
    )

    if not HEX_128_RE.match(net_key):
        raise ValueError("net_key must be 32 hex characters")
    if not HEX_128_RE.match(app_key):
        raise ValueError("app_key must be 32 hex characters")

    raw_lights = data.get(CONF_LIGHTS)
    if not isinstance(raw_lights, list) or not raw_lights:
        raise ValueError("lights must be a non-empty list")

    lights: list[AmaranLightConfig] = []
    for index, raw_light in enumerate(raw_lights, start=1):
        if not isinstance(raw_light, dict):
            raise ValueError(f"lights[{index}] must be an object")
        name = str(raw_light.get("name") or f"Amaran Light {index}").strip()
        mac = normalize_mac(str(raw_light.get(CONF_MAC) or ""))
        address_value: Any = raw_light.get(CONF_MESH_ADDRESS, raw_light.get("address"))
        if isinstance(address_value, str) and address_value.lower().startswith("0x"):
            mesh_address = int(address_value, 16)
        else:
            mesh_address = int(address_value)
        if mesh_address <= 0 or mesh_address > 0xFFFF:
            raise ValueError(f"lights[{index}].mesh_address must be 1..65535")
        min_kelvin = int(raw_light.get("min_kelvin", raw_light.get("minKelvin", 2500)))
        max_kelvin = int(raw_light.get("max_kelvin", raw_light.get("maxKelvin", 7500)))
        if min_kelvin <= 0 or max_kelvin < min_kelvin:
            raise ValueError(
                f"lights[{index}] has an invalid CCT range: "
                f"{min_kelvin}K..{max_kelvin}K"
            )
        lights.append(
            AmaranLightConfig(
                name=name,
                mac=mac,
                mesh_address=mesh_address,
                min_kelvin=min_kelvin,
                max_kelvin=max_kelvin,
                supports_hs=bool(raw_light.get("supports_hs", raw_light.get("supportsHs", True))),
                supports_gm=bool(raw_light.get("supports_gm", raw_light.get("supportsGm", True))),
            )
        )

    proxy_candidates: list[str] = []
    if isinstance(raw_proxy_candidates, str) and raw_proxy_candidates.strip():
        proxy_candidates.append(normalize_mac(raw_proxy_candidates))
    elif isinstance(raw_proxy_candidates, list):
        for raw_candidate in raw_proxy_candidates:
            proxy_candidates.append(normalize_mac(str(raw_candidate)))
    elif raw_proxy_candidates:
        raise ValueError("proxy_candidates must be a list of MAC addresses")

    relay_hub = normalize_mac(raw_relay_hub) if raw_relay_hub else lights[0].mac
    return AmaranConfig(
        net_key=net_key,
        app_key=app_key,
        relay_hub=relay_hub,
        lights=lights,
        proxy_candidates=list(dict.fromkeys(proxy_candidates)),
    )


def config_to_storage(config: AmaranConfig) -> dict[str, Any]:
    """Convert parsed config to Home Assistant config entry data."""
    return {
        CONF_NET_KEY: config.net_key,
        CONF_APP_KEY: config.app_key,
        CONF_RELAY_HUB: config.relay_hub,
        CONF_PROXY_CANDIDATES: config.proxy_candidates,
        CONF_LIGHTS: [
            {
                "name": light.name,
                CONF_MAC: light.mac,
                CONF_MESH_ADDRESS: light.mesh_address,
                "min_kelvin": light.min_kelvin,
                "max_kelvin": light.max_kelvin,
                "supports_hs": light.supports_hs,
                "supports_gm": light.supports_gm,
            }
            for light in config.lights
        ],
    }


def config_from_storage(data: dict[str, Any]) -> AmaranConfig:
    """Parse Home Assistant config entry data."""
    return parse_config_json(json.dumps(data))
