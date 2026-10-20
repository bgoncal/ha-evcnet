"""Fixtures for the EVC-net tests."""

from collections.abc import Generator
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evcnet.api import (
    Channel,
    ChargePoint,
    ChargePointStatus,
    Transaction,
)
from custom_components.evcnet.const import CONF_COOKIES, CONF_TWO_FACTOR, DOMAIN
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME

FIXTURES = Path(__file__).parent / "fixtures"
CHARGE_POINT_ID = "1234567890"
BASE_URL = "https://50five-snl.evc-net.com"
USERNAME = "user@example.com"
PASSWORD = "secret"
COOKIES = {"PHPSESSID": "saved-session", "SERVERID": "www_1"}


def load_fixture(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


def status_from_fixture(name: str) -> ChargePointStatus:
    """Build a charge point status from an overview fixture."""
    channels = [Channel.from_overview(row) for row in load_fixture(name)]
    return ChargePointStatus(channels={ch.channel: ch for ch in channels})


def transactions_from_fixture() -> list[Transaction]:
    """Build the charging history from the transactions fixture."""
    return [Transaction.from_row(row) for row in load_fixture("transactions.json")]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the custom integration."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=f"50five-snl.evc-net.com:{USERNAME}",
        data={
            CONF_URL: BASE_URL,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_COOKIES: COOKIES,
            CONF_TWO_FACTOR: True,
        },
    )


@pytest.fixture
def overview_fixture() -> str:
    """Return the overview fixture used by the mocked client."""
    return "overview_offline.json"


@pytest.fixture
def mock_client_cls(overview_fixture: str) -> Generator[MagicMock]:
    """Mock the EVC-net client class used by setup and the config flow."""
    with (
        patch("custom_components.evcnet.EvcNetClient", autospec=True) as client_cls,
        patch("custom_components.evcnet.config_flow.EvcNetClient", new=client_cls),
    ):
        client = client_cls.return_value
        client.cookies = COOKIES
        client.two_factor_destination = "u***@e***.com"
        client.charge_points.return_value = [
            ChargePoint(
                id=CHARGE_POINT_ID,
                name=None,
                address="Examplestraat 1",
                software_version="1.13.0.1@2.4.6",
                channels=("1",),
            )
        ]
        client.status.return_value = {
            CHARGE_POINT_ID: status_from_fixture(overview_fixture)
        }
        client.transactions.return_value = {
            CHARGE_POINT_ID: transactions_from_fixture()
        }
        yield client_cls


@pytest.fixture
def mock_client(mock_client_cls: MagicMock) -> AsyncMock:
    """Return the mocked EVC-net client."""
    return mock_client_cls.return_value
