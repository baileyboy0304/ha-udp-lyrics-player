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


def _config_schema(defaults: dict) -> vol.Schema:
    """Return a schema using only HA-serialisable validators.

    Custom validation (URL prefix, IP/hostname) is done manually in the
    step handlers so voluptuous_serialize can render the form without error.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_PLAYER_NAME,
                default=defaults.get(CONF_PLAYER_NAME, ""),
            ): cv.string,
            vol.Required(
                CONF_SENDSPIN_SERVER_URL,
                default=defaults.get(CONF_SENDSPIN_SERVER_URL, DEFAULT_SENDSPIN_URL),
            ): cv.string,
            vol.Required(
                CONF_UDP_HOST,
                default=defaults.get(CONF_UDP_HOST, ""),
            ): cv.string,
            vol.Required(
                CONF_UDP_PORT,
                default=defaults.get(CONF_UDP_PORT, DEFAULT_UDP_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        }
    )


def _validate_inputs(user_input: dict) -> dict[str, str]:
    """Validate fields that cannot be expressed as serialisable schema types.

    Returns a dict of field → error-key for any failures.
    """
    errors: dict[str, str] = {}

    # WebSocket URL must start with ws:// or wss://
    url = user_input.get(CONF_SENDSPIN_SERVER_URL, "").strip()
    if not (url.startswith("ws://") or url.startswith("wss://")):
        errors[CONF_SENDSPIN_SERVER_URL] = "invalid_ws_url"

    # UDP host must be a non-empty IP address or hostname
    host = user_input.get(CONF_UDP_HOST, "").strip()
    if not host:
        errors[CONF_UDP_HOST] = "invalid_host"
    else:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Not a bare IP literal – accept as a hostname; reject empty (caught above)
            pass

    return errors


class UDPLyricsPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a UDP Lyrics Player entry."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create the options flow."""
        return UDPLyricsPlayerOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_inputs(user_input)

            if not errors:
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
        )


class UDPLyricsPlayerOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to edit all four fields after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        current = dict(self._config_entry.data)

        if user_input is not None:
            errors = _validate_inputs(user_input)

            if not errors:
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
