"""UDP Lyrics Player – Home Assistant custom integration.

A Sendspin-compatible dummy media player that joins a Sendspin group for
synchronised playback and forwards the received audio stream over UDP (16-bit
mono 16 kHz PCM) to a configurable IP/port, typically the Music Companion
lyrics-recognition tagging service.
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, DATA_PLAYER

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player"]


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up a UDP Lyrics Player entry and forward it to the media_player platform."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config_entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry and tear down the media_player platform."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )
    if unload_ok and config_entry.entry_id in hass.data.get(DOMAIN, {}):
        del hass.data[DOMAIN][config_entry.entry_id]
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reload the config entry (called by HA on options update)."""
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)
