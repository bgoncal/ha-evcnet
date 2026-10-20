"""Config flow for EVC-net."""

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    DEFAULT_BASE_URL,
    EvcNetAuthError,
    EvcNetClient,
    EvcNetError,
    EvcNetInvalidCode,
    EvcNetTwoFactorRequired,
)
from .const import CONF_CODE, CONF_COOKIES, CONF_TWO_FACTOR, DOMAIN, LOGGER

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_TWO_FACTOR = "two_factor"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_BASE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
STEP_REAUTH_SCHEMA = vol.Schema({vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR})
STEP_TWO_FACTOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CODE): TextSelector(
            TextSelectorConfig(autocomplete="one-time-code")
        )
    }
)


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"https://{url}"
    return str(URL(url).origin())


class EvcNetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EVC-net."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}
        self._client: EvcNetClient | None = None
        self._session: aiohttp.ClientSession | None = None

    @callback
    def async_remove(self) -> None:
        """Release the portal session when the flow goes away."""
        self._release_session()

    def _release_session(self) -> None:
        if self._session is not None:
            self._session.detach()
            self._session = None

    async def _async_login(self) -> str | None:
        """Log in with the collected data; return an error key or _TWO_FACTOR."""
        self._release_session()
        self._session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=aiohttp.DummyCookieJar()
        )
        self._client = EvcNetClient(
            self._session,
            self._data[CONF_USERNAME],
            self._data[CONF_PASSWORD],
            self._data[CONF_URL],
        )
        try:
            await self._client.login()
        except EvcNetTwoFactorRequired:
            return _TWO_FACTOR
        except EvcNetAuthError:
            return "invalid_auth"
        except EvcNetError:
            return "cannot_connect"
        except Exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error while logging in to EVC-net")
            return "unknown"
        self._data[CONF_TWO_FACTOR] = False
        return None

    def _finish(self) -> ConfigFlowResult:
        assert self._client is not None
        data = {**self._data, CONF_COOKIES: self._client.cookies}
        self._release_session()
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )
        return self.async_create_entry(title=data[CONF_USERNAME], data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(f"{URL(url).host}:{username.lower()}")
            self._abort_if_unique_id_configured()
            self._data = {
                CONF_URL: url,
                CONF_USERNAME: username,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            if (error := await self._async_login()) is None:
                return self._finish()
            if error == _TWO_FACTOR:
                return await self.async_step_two_factor()
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_two_factor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the code the portal emailed."""
        assert self._client is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._client.verify_code(user_input[CONF_CODE])
            except EvcNetInvalidCode:
                errors["base"] = "invalid_code"
            except EvcNetAuthError:
                self._release_session()
                return self.async_abort(reason="two_factor_expired")
            except EvcNetError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected error while verifying the EVC-net code")
                errors["base"] = "unknown"
            else:
                self._data[CONF_TWO_FACTOR] = True
                return self._finish()
        return self.async_show_form(
            step_id="two_factor",
            data_schema=STEP_TWO_FACTOR_SCHEMA,
            description_placeholders={
                "destination": self._client.two_factor_destination
                or "your email address"
            },
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a rejected password or an ended session."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in again, keeping the saved password unless a new one is given."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_URL: entry.data[CONF_URL],
                CONF_USERNAME: entry.data[CONF_USERNAME],
                CONF_PASSWORD: user_input.get(CONF_PASSWORD)
                or entry.data[CONF_PASSWORD],
            }
            if (error := await self._async_login()) is None:
                return self._finish()
            if error == _TWO_FACTOR:
                return await self.async_step_two_factor()
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
