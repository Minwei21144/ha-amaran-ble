"""Config flow for Amaran BLE."""

from __future__ import annotations

from typing import Any

try:
    import voluptuous as vol
except ModuleNotFoundError:  # pragma: no cover - allows helper tests outside HA.
    class _VoluptuousFallback:
        @staticmethod
        def Required(key: str) -> str:
            return key

        @staticmethod
        def Schema(schema: dict[str, object]) -> dict[str, object]:
            return schema

    vol = _VoluptuousFallback()  # type: ignore[assignment]

try:
    from homeassistant import config_entries
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.data_entry_flow import FlowResult
except ModuleNotFoundError:  # pragma: no cover - allows helper tests outside HA.
    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__()

    class _OptionsFlow:
        pass

    class _ConfigEntries:
        ConfigFlow = _ConfigFlow
        OptionsFlow = _OptionsFlow

    config_entries = _ConfigEntries()  # type: ignore[assignment]
    BluetoothServiceInfoBleak = object  # type: ignore[assignment]
    ConfigEntry = object  # type: ignore[assignment]
    FlowResult = dict[str, Any]  # type: ignore[misc,assignment]

from .const import (
    CONF_CONFIG_JSON,
    CONF_LAST_SUCCESSFUL_PROXY,
    CONF_LIGHTS,
    CONF_MAC,
    CONF_PROXY_CANDIDATES,
    CONF_RELAY_HUB,
    DOMAIN,
    MESH_PROXY_SERVICE_UUID,
)
from .models import AmaranConfig, config_to_storage, normalize_mac, parse_config_json

IMPORT_SCHEMA = vol.Schema({vol.Required(CONF_CONFIG_JSON): str})


class AmaranBleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an Amaran BLE config flow."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> AmaranBleOptionsFlow:
        """Return the options flow for this config entry."""
        return AmaranBleOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle manual setup."""
        return await self._show_import_form(user_input)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle Bluetooth discovery."""
        service_uuids = {uuid.lower() for uuid in discovery_info.service_uuids}
        if MESH_PROXY_SERVICE_UUID not in service_uuids and "00001828-0000-1000-8000-00805f9b34fb" not in service_uuids:
            return self.async_abort(reason="not_amaran_mesh_proxy")
        try:
            discovered_mac = normalize_mac(discovery_info.address)
        except ValueError:
            discovered_mac = discovery_info.address
        if self._discovered_address_is_configured(
            discovery_info.address,
            (
                self._entry_known_mac_sources(entry)
                for entry in self._async_current_entries()
            ),
        ):
            await self.async_set_unique_id(discovered_mac)
            self._abort_if_unique_id_configured()
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(discovered_mac)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = self._discovery_title(
            discovery_info.name,
            discovery_info.address,
        )
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self._show_import_form()

    async def _show_import_form(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = parse_config_json(user_input[CONF_CONFIG_JSON])
            except ValueError:
                errors["base"] = "invalid_config_json"
            else:
                try:
                    config = self._config_for_discovered_device(config)
                except ValueError:
                    errors["base"] = "device_not_in_config_json"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=IMPORT_SCHEMA,
                        errors=errors,
                        description_placeholders=self._description_placeholders(),
                    )

                unique_id = config.lights[0].mac if self._discovered_address else config.relay_hub
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._abort_matching_discovery_flows(config)
                title = (
                    config.lights[0].name
                    if self._discovered_address
                    else f"Amaran BLE {config.relay_hub}"
                )
                return self.async_create_entry(
                    title=title,
                    data=config_to_storage(config),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=IMPORT_SCHEMA,
            errors=errors,
            description_placeholders=self._description_placeholders(),
        )

    def _description_placeholders(self) -> dict[str, str]:
        discovered = self._discovered_name or self._discovered_address or ""
        return {
            "discovered": discovered,
            "example": (
                '{"net_key":"...","app_key":"...",'
                '"lights":[{"name":"amaran T2c #1","mac":"AA:BB:CC:11:22:33",'
                '"mesh_address":5,"min_kelvin":2500,"max_kelvin":7500}]}'
            ),
        }

    def _discovery_title(self, name: str | None, address: str) -> str:
        """Build a human-readable discovery title with a MAC suffix."""
        base_name = (name or "Mesh Device").strip()
        try:
            mac = normalize_mac(address)
        except ValueError:
            return f"{base_name} {address}".strip()
        suffix = ":".join(mac.split(":")[-3:])
        if suffix.lower() in base_name.lower().replace("-", ":"):
            return base_name
        return f"{base_name} {suffix}"

    def _discovered_address_is_configured(
        self,
        address: str,
        entries: object,
    ) -> bool:
        """Return whether a discovered MAC already belongs to an entry."""
        try:
            discovered_mac = normalize_mac(address)
        except ValueError:
            return False
        discovered_raw = discovered_mac.replace(":", "")
        for data in entries:
            for known_mac in self._known_macs_from_entry_data(data):
                known_raw = known_mac.replace(":", "")
                if known_raw == discovered_raw:
                    return True
                if known_raw[-6:] == discovered_raw[-6:]:
                    return True
        return False

    def _entry_known_mac_sources(self, entry: ConfigEntry) -> dict[str, object]:
        """Return all entry data/options locations that can contain proxy MACs."""
        data = dict(getattr(entry, "data", {}) or {})
        options = dict(getattr(entry, "options", {}) or {})
        sources = dict(data)
        if (last_proxy := options.get(CONF_LAST_SUCCESSFUL_PROXY)) is not None:
            raw_proxy_candidates = sources.get(CONF_PROXY_CANDIDATES, [])
            candidates = (
                list(raw_proxy_candidates)
                if isinstance(raw_proxy_candidates, list)
                else [raw_proxy_candidates]
                if raw_proxy_candidates
                else []
            )
            candidates.append(last_proxy)
            sources[CONF_PROXY_CANDIDATES] = candidates
        return sources

    def _known_macs_from_entry_data(self, data: object) -> list[str]:
        """Return all MAC addresses stored in one config entry."""
        if not isinstance(data, dict):
            return []
        raw_values: list[object] = []
        raw_values.append(data.get(CONF_RELAY_HUB))
        raw_proxy_candidates = data.get(CONF_PROXY_CANDIDATES, [])
        if isinstance(raw_proxy_candidates, list):
            raw_values.extend(raw_proxy_candidates)
        elif raw_proxy_candidates:
            raw_values.append(raw_proxy_candidates)
        raw_lights = data.get(CONF_LIGHTS, [])
        if isinstance(raw_lights, list):
            for raw_light in raw_lights:
                if isinstance(raw_light, dict):
                    raw_values.append(raw_light.get(CONF_MAC))

        known_macs: list[str] = []
        for raw_value in raw_values:
            if raw_value is None:
                continue
            try:
                known_macs.append(normalize_mac(str(raw_value)))
            except ValueError:
                continue
        return list(dict.fromkeys(known_macs))

    def _abort_matching_discovery_flows(self, config: AmaranConfig) -> None:
        """Remove stale Bluetooth discovery cards covered by an imported mesh."""
        flow_manager = self.hass.config_entries.flow
        entry_data = config_to_storage(config)
        current_flow_id = getattr(self, "flow_id", None)

        for progress in flow_manager.async_progress_by_handler(DOMAIN):
            if not isinstance(progress, dict):
                continue
            flow_id = progress.get("flow_id")
            if not isinstance(flow_id, str) or flow_id == current_flow_id:
                continue
            context = progress.get("context")
            if not isinstance(context, dict) or context.get("source") != "bluetooth":
                continue
            unique_id = context.get("unique_id")
            if not isinstance(unique_id, str):
                continue
            if self._discovered_address_is_configured(unique_id, [entry_data]):
                flow_manager.async_abort(flow_id)

    def _config_for_discovered_device(self, config: AmaranConfig) -> AmaranConfig:
        """Limit imported config to the discovered Bluetooth device when present."""
        if not self._discovered_address:
            return config

        discovered_mac = normalize_mac(self._discovered_address)
        discovered_raw = discovered_mac.replace(":", "")
        for light in config.lights:
            light_raw = light.mac.replace(":", "")
            if light_raw == discovered_raw or light_raw.endswith(discovered_raw[-6:]):
                return AmaranConfig(
                    net_key=config.net_key,
                    app_key=config.app_key,
                    relay_hub=light.mac,
                    lights=[light],
                    proxy_candidates=list(
                        dict.fromkeys(
                            [
                                *config.proxy_candidates,
                                *(candidate.mac for candidate in config.lights),
                            ]
                        )
                    ),
                )

        raise ValueError(f"Discovered device {discovered_mac} was not found in config JSON")


class AmaranBleOptionsFlow(config_entries.OptionsFlow):
    """Handle Amaran BLE reconfiguration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Update the stored mesh config JSON."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = parse_config_json(user_input[CONF_CONFIG_JSON])
            except ValueError:
                errors["base"] = "invalid_config_json"
            else:
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data=config_to_storage(config),
                )
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=IMPORT_SCHEMA,
            errors=errors,
            description_placeholders={
                "example": (
                    '{"net_key":"...","app_key":"...",'
                    '"proxy_candidates":["AA:BB:CC:11:22:33"],'
                    '"lights":[{"name":"amaran T2c #1","mac":"AA:BB:CC:11:22:33",'
                    '"mesh_address":5,"min_kelvin":2500,"max_kelvin":7500}]}'
                ),
            },
        )
