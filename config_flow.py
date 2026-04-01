import ipaddress
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_PLAYER_NAME,
    CONF_SENDSPIN_SERVER_URL,
    CONF_UDP_HOST,
    CONF_UDP_PORT,
    DEFAULT_UDP_PORT,
    DEFAULT_SENDSPIN_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _validate_ip(value: str) -> str:
    """Validate that the value is a valid IP address or hostname."""
    value = value.strip()
    if not value:
        raise vol.Invalid("IP address / hostname cannot be empty")
    # Allow hostnames as well – only reject values that look like IPs but are invalid
    try:
        ipaddress.ip_address(value)
    except ValueError:
        # Not an IP literal – accept as a hostname (non-empty check already done above)
        pass
    return value


def _validate_ws_url(value: str) -> str:
    """Validate a WebSocket URL (ws:// or wss://)."""
    value = value.strip()
    if not (value.startswith("ws://") or value.startswith("wss://")):
        raise vol.Invalid("URL must start with ws:// or wss://")
    return value


def _config_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_PLAYER_NAME,
                default=defaults.get(CONF_PLAYER_NAME, ""),
            ): cv.string,
            vol.Required(
                CONF_SENDSPIN_SERVER_URL,
                default=defaults.get(CONF_SENDSPIN_SERVER_URL, DEFAULT_SENDSPIN_URL),
            ): vol.All(cv.string, _validate_ws_url),
            vol.Required(
                CONF_UDP_HOST,
                default=defaults.get(CONF_UDP_HOST, ""),
            ): vol.All(cv.string, _validate_ip),
            vol.Required(
                CONF_UDP_PORT,
                default=defaults.get(CONF_UDP_PORT, DEFAULT_UDP_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        }
    )


class UDPLyricsPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a UDP Lyrics Player entry."""

    VERSION = 1

    # ── Options cog ──────────────────────────────────────────────────────────
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return UDPLyricsPlayerOptionsFlow(config_entry)

    # ── Initial user step ─────────────────────────────────────────────────────
    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            # Prevent duplicate player names
            for entry in self._async_current_entries():
                if entry.data.get(CONF_PLAYER_NAME) == user_input[CONF_PLAYER_NAME]:
                    errors[CONF_PLAYER_NAME] = "name_exists"
                    break

            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_PLAYER_NAME],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_config_schema(user_input or {}),
            errors=errors,
            description_placeholders={
                "udp_note": (
                    "The destination IP and port are where audio will be forwarded "
                    "for lyrics recognition (e.g. the Music Companion tagging service)."
                )
            },
        )


class UDPLyricsPlayerOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to edit all four fields after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        current = dict(self._config_entry.data)

        if user_input is not None:
            # Merge updated values back into the config entry data
            current.update(user_input)
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=current
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_config_schema(current),
            errors=errors,
        )
