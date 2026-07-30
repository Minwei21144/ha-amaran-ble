"""Amaran BLE integration."""

from __future__ import annotations

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant

    PLATFORMS: list[Platform] = [
        Platform.LIGHT,
        Platform.NUMBER,
        Platform.SWITCH,
        Platform.SENSOR,
    ]
except ModuleNotFoundError:  # pragma: no cover - allows protocol-only tests outside HA.
    ConfigEntry = object  # type: ignore[assignment]
    HomeAssistant = object  # type: ignore[assignment]
    PLATFORMS: list[str] = ["light", "number", "switch", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Amaran BLE from a config entry."""
    from .const import DOMAIN
    from .controller import AmaranBleController
    from .models import config_from_storage

    hass.data.setdefault(DOMAIN, {})
    controller = AmaranBleController(
        hass,
        config_from_storage(dict(entry.data)),
    )
    hass.data[DOMAIN][entry.entry_id] = controller
    await controller.async_start(entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await controller.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Amaran BLE config entry."""
    from .const import DOMAIN

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        controller = hass.data[DOMAIN].pop(entry.entry_id, None)
        if controller is not None:
            await controller.async_stop()
    return unload_ok
