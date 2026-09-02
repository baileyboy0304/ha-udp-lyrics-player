"""UDP Lyrics Player – Home Assistant custom integration.

A Sendspin-compatible dummy media player that joins a Sendspin group for
synchronised playback and forwards the received audio stream over UDP (16-bit
mono 16 kHz PCM) to a configurable IP/port, typically LyricsMachine's
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

    # The options flow writes the edited fields back into config_entry.data, but
    # nothing acts on that unless an update listener is registered — without this
    # a changed Sendspin URL / UDP target only takes effect after a full HA
    # restart, because the running entity keeps its constructor-time values.
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_reload_entry)
    )

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
    """Reload the config entry (registered as the options update listener).

    Delegates to the config-entry manager rather than calling unload/setup
    directly so HA keeps its own entry state machine consistent.
    """
    await hass.config_entries.async_reload(config_entry.entry_id)
