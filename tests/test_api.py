"""Tests for the EVC-net API client against a fake portal."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import json
import secrets
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.evcnet.api import (
    Card,
    ChannelState,
    ChargePointAction,
    EvcNetAuthError,
    EvcNetClient,
    EvcNetConnectionError,
    EvcNetInvalidCode,
    EvcNetSessionExpired,
    EvcNetTwoFactorRequired,
    _number,
    channel_state,
    parse_status,
)

from .conftest import CHARGE_POINT_ID, PASSWORD, USERNAME, load_fixture

pytestmark = pytest.mark.usefixtures("socket_enabled")

CODE = "123456"
TWO_FACTOR_PAGE = """<html><head><title>50five</title></head><body>
<p>A verification code has been sent to u***@e***.com. Enter the code below.</p>
<form method='POST' action='/2fa_check' enctype='multipart/form-data'>
<input type='hidden' name='_token' id='_token' value='{token}'>
<input placeholder='Verification code' name='_auth_code' id='otpCodeInput' type='text'
 inputmode='numeric' pattern='[0-9]{{6}}' maxlength='6' autocomplete='one-time-code'>
<button type='submit' name='VerifyOtp' id='VerifyOtp' value='Verify'>Verify</button>
</form>
<form method='POST' action='/Login/CustomerOtp/Scheb/Resend' enctype='multipart/form-data'>
<input type='hidden' name='_token' id='_token' value='resend-token'>
<button type='submit' name='ResendOtp' id='ResendOtp' value='Resend code'>Resend code</button>
</form></body></html>"""


CARDS_PAGE = """<html><body><table class='table'>
<thead><tr><th>Card ID</th><th>Expire date</th><th>Reference</th><th></th></tr></thead>
<tbody>
<tr><td>NL-ABC-C12345678-X</td><td></td><td>Home</td>
<td><a href='/Cards/DeactivateCard/501'>Deactivate</a></td></tr>
<tr><td>NL-XYZ-C87654321-Y</td><td></td><td></td>
<td><a href='/Cards/DeactivateCard/502'>Deactivate</a></td></tr>
<tr><td>NL-OLD-C00000000-Z</td><td>01-01-2025</td><td></td>
<td><a href='/Cards/ActivateCard/503'>Activate</a></td></tr>
</tbody></table></body></html>"""


class FakePortal:
    """Mimics the behaviour of the real EVC-net portal seen in live logins.

    Logging in regenerates the session id. Without 2FA the session becomes
    usable once the landing page is loaded; with 2FA the landing page sends
    the browser to /2fa until the emailed code is posted to /2fa_check.
    Redirects point at the other scheme (the real portal sends http and
    relies on HSTS), so following them literally fails.
    """

    def __init__(self) -> None:
        """Initialize the fake portal."""
        self.sessions: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.two_factor = False
        self.logins = 0
        self.codes_sent = 0
        self.landings = 0
        self.overview: list[dict[str, Any]] = load_fixture("overview_offline.json")
        self.html_ajax = False
        self.reject_with_redirect = False
        self.ajax_ignores_sessions = False
        self.empty_answers = 0
        self.actions: list[dict[str, Any]] = []
        self.app = web.Application()
        self.app.router.add_get("/Login/Login", self._login_page)
        self.app.router.add_post("/Login/Login", self._login)
        self.app.router.add_get("/Overview", self._landing)
        self.app.router.add_get("/2fa", self._two_factor_page)
        self.app.router.add_post("/2fa_check", self._two_factor_check)
        self.app.router.add_post("/Login/CustomerOtp/Scheb/Resend", self._resend)
        self.app.router.add_get("/api/ajax", self._ajax)
        self.app.router.add_get("/Cards/List", self._cards_page)

    def expire_sessions(self) -> None:
        """Forget every session, like the portal does after a day."""
        self.sessions.clear()

    @staticmethod
    def _wrong_scheme(request: web.Request, path: str) -> str:
        return f"https://{request.host}{path}"

    def _state(self, request: web.Request) -> str | None:
        return self.sessions.get(request.cookies.get("PHPSESSID", ""))

    def _redirect(
        self, request: web.Request, path: str, session_id: str | None = None
    ) -> web.HTTPFound:
        response = web.HTTPFound(self._wrong_scheme(request, path))
        if session_id:
            response.set_cookie("PHPSESSID", session_id, max_age=86400)
        return response

    def _new_session(self, state: str) -> str:
        session_id = secrets.token_hex(13)
        self.sessions[session_id] = state
        return session_id

    async def _login_page(self, request: web.Request) -> web.Response:
        response = web.Response(text="<form></form>", content_type="text/html")
        if self._state(request) is None:
            response.set_cookie("PHPSESSID", self._new_session("anonymous"))
        if "SERVERID" not in request.cookies:
            response.set_cookie("SERVERID", "www_1")
        return response

    async def _login(self, request: web.Request) -> web.Response:
        assert request.content_type == "multipart/form-data"
        assert self._state(request) == "anonymous"
        assert request.cookies.get("SERVERID") == "www_1"
        form = await request.post()
        self.logins += 1
        if (form["emailField"], form["passwordField"]) != (USERNAME, PASSWORD):
            if self.reject_with_redirect:
                raise self._redirect(request, "/Login/Login")
            return web.Response(text="<form>Invalid</form>", content_type="text/html")
        if self.two_factor:
            self.codes_sent += 1
        state = "needs_code" if self.two_factor else "pending"
        raise self._redirect(request, "/Overview", self._new_session(state))

    async def _landing(self, request: web.Request) -> web.Response:
        state = self._state(request)
        if state == "needs_code":
            raise self._redirect(request, "/2fa")
        if state not in ("pending", "active"):
            raise self._redirect(request, "/Login/Login")
        self.landings += 1
        self.sessions[request.cookies["PHPSESSID"]] = "active"
        response = web.Response(
            text="<title>Overview</title>", content_type="text/html"
        )
        response.set_cookie("PHPSESSID", request.cookies["PHPSESSID"], max_age=86400)
        return response

    async def _two_factor_page(self, request: web.Request) -> web.Response:
        if self._state(request) != "needs_code":
            raise self._redirect(request, "/Login/Login")
        token = self.tokens[request.cookies["PHPSESSID"]] = secrets.token_hex(16)
        return web.Response(
            text=TWO_FACTOR_PAGE.format(token=token), content_type="text/html"
        )

    async def _two_factor_check(self, request: web.Request) -> web.Response:
        assert request.content_type == "multipart/form-data"
        if self._state(request) != "needs_code":
            raise self._redirect(request, "/Login/Login")
        form = await request.post()
        assert form["VerifyOtp"] == "Verify"
        assert form["_token"] == self.tokens[request.cookies["PHPSESSID"]]
        if form["_auth_code"] != CODE:
            raise self._redirect(request, "/2fa")
        del self.sessions[request.cookies["PHPSESSID"]]
        # The real portal redirects to https here, with a query string.
        response = web.HTTPFound("/Overview?Menu=%2FOverview")
        response.set_cookie("PHPSESSID", self._new_session("active"), max_age=86400)
        raise response

    async def _cards_page(self, request: web.Request) -> web.Response:
        if self._state(request) != "active":
            raise self._redirect(request, "/Login/Login")
        return web.Response(text=CARDS_PAGE, content_type="text/html")

    async def _resend(self, request: web.Request) -> web.Response:
        raise AssertionError("The client must never ask for another code")

    async def _ajax(self, request: web.Request) -> web.Response:
        if self.html_ajax:
            return web.Response(
                text="<html>Maintenance</html>", content_type="text/html"
            )
        if self.ajax_ignores_sessions or self._state(request) != "active":
            return web.Response(text="[]", content_type="text/html")
        if self.empty_answers:
            self.empty_answers -= 1
            return web.Response(text="[]", content_type="text/html")
        requests = json.loads(request.query["requests"])
        results = [self._handle(requests[str(i)]) for i in range(len(requests))]
        return web.Response(text=json.dumps(results), content_type="text/html")

    def _handle(self, req: dict[str, Any]) -> Any:
        return {
            "networkOverview": lambda: load_fixture("network_overview.json"),
            "overview": lambda: self.overview,
            "transactions": lambda: load_fixture("transactions.json"),
            "action": lambda: self.actions.append(req["params"]) or True,
        }[req["method"]]()


@pytest.fixture
async def portal() -> AsyncGenerator[tuple[FakePortal, str]]:
    """Run the fake portal."""
    fake = FakePortal()
    server = TestServer(fake.app)
    await server.start_server()
    yield fake, str(server.make_url("")).rstrip("/")
    await server.close()


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    """Return a session without a cookie jar, as the integration uses."""
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar()
    ) as client_session:
        yield client_session


async def test_charge_points(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test logging in and listing charge points."""
    fake, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    (charge_point,) = await client.charge_points()

    assert charge_point.id == CHARGE_POINT_ID
    assert charge_point.software_version == "1.13.0.1@2.4.6"
    assert charge_point.channels == ("1",)
    assert (fake.logins, fake.landings) == (1, 1)
    assert set(client.cookies) == {"PHPSESSID", "SERVERID"}


async def test_status_offline(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test the real payload of a charge point without communication."""
    _, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    status = (await client.status([CHARGE_POINT_ID]))[CHARGE_POINT_ID]

    channel = status.channels["1"]
    assert channel.state is ChannelState.OFFLINE
    assert channel.flags == ("no_communication",)
    assert channel.notification == "No communication"
    assert channel.connector == "IEC-62196-T2"
    assert channel.power_kw is None
    assert channel.in_transaction is False
    assert status.online is False
    assert status.total_energy_kwh is None


async def test_status_charging(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test a charging channel."""
    fake, url = portal
    fake.overview = load_fixture("overview_charging.json")
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    status = (await client.status([CHARGE_POINT_ID]))[CHARGE_POINT_ID]

    channel = status.channels["1"]
    assert channel.state is ChannelState.CHARGING
    assert channel.flags == ("relay1_active", "lock_engaged", "transaction")
    assert channel.power_kw == 7.36
    assert channel.session_energy_kwh == 3.21
    assert channel.session_minutes == 65
    assert channel.card_id == "NL-ABC-C12345678-X"
    assert channel.in_transaction is True
    assert status.online is True


async def test_transactions(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test reading the charging history."""
    _, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    transactions = (await client.transactions([CHARGE_POINT_ID]))[CHARGE_POINT_ID]

    assert [tx.id for tx in transactions] == ["1001", "1000", "999", "1002"]
    latest = transactions[0]
    assert latest.energy_kwh == 17.03
    assert latest.channel == "1"
    assert latest.start == datetime(2026, 10, 6, 7, 42, 13, tzinfo=UTC)
    assert latest.end == datetime(2026, 10, 7, 10, 41, 5, tzinfo=UTC)
    assert transactions[-1].end is None


async def test_two_factor_login(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test a login that needs the emailed code, including a typo."""
    fake, url = portal
    fake.two_factor = True
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    with pytest.raises(EvcNetTwoFactorRequired):
        await client.login()
    assert client.two_factor_destination == "u***@e***.com"

    with pytest.raises(EvcNetInvalidCode):
        await client.verify_code("000000")
    await client.verify_code(f" {CODE} ")

    assert CHARGE_POINT_ID in await client.status([CHARGE_POINT_ID])
    assert (fake.logins, fake.codes_sent) == (1, 1)


async def test_verify_code_without_login(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test submitting a code when no verification is pending."""
    _, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    with pytest.raises(EvcNetAuthError):
        await client.verify_code(CODE)


async def test_restored_session_skips_login(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test that saved cookies are enough after a restart."""
    fake, url = portal
    fake.two_factor = True
    first = EvcNetClient(session, USERNAME, PASSWORD, url)
    with pytest.raises(EvcNetTwoFactorRequired):
        await first.login()
    await first.verify_code(CODE)

    second = EvcNetClient(
        session, USERNAME, PASSWORD, url, cookies=first.cookies, auto_login=False
    )

    assert CHARGE_POINT_ID in await second.status([CHARGE_POINT_ID])
    assert (fake.logins, fake.codes_sent) == (1, 1)


async def test_expired_session_logs_in_again(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test that an ended session is renewed when no code is needed."""
    fake, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)
    await client.status([CHARGE_POINT_ID])

    fake.expire_sessions()

    assert CHARGE_POINT_ID in await client.status([CHARGE_POINT_ID])
    assert fake.logins == 2


async def test_expired_session_without_auto_login(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test an ended session does not trigger a code email by itself."""
    fake, url = portal
    fake.two_factor = True
    client = EvcNetClient(session, USERNAME, PASSWORD, url, auto_login=False)
    with pytest.raises(EvcNetTwoFactorRequired):
        await client.login()
    await client.verify_code(CODE)

    fake.expire_sessions()

    with pytest.raises(EvcNetSessionExpired):
        await client.status([CHARGE_POINT_ID])
    assert (fake.logins, fake.codes_sent) == (1, 1)


async def test_no_session_without_auto_login(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test a client without cookies that may not log in by itself."""
    fake, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url, auto_login=False)

    with pytest.raises(EvcNetSessionExpired):
        await client.status([CHARGE_POINT_ID])
    assert fake.logins == 0


async def test_empty_answer_with_live_session_is_retried(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test a one-off empty answer does not end a working session."""
    fake, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url, auto_login=False)
    fake.two_factor = True
    with pytest.raises(EvcNetTwoFactorRequired):
        await client.login()
    await client.verify_code(CODE)

    fake.empty_answers = 1

    assert CHARGE_POINT_ID in await client.status([CHARGE_POINT_ID])
    assert fake.logins == 1


@pytest.mark.parametrize(
    "reject_with_redirect",
    [
        pytest.param(False, id="login_form_shown_again"),
        pytest.param(True, id="redirect_back_to_login"),
    ],
)
async def test_wrong_password(
    portal: tuple[FakePortal, str],
    session: aiohttp.ClientSession,
    reject_with_redirect: bool,
) -> None:
    """Test rejected credentials."""
    fake, url = portal
    fake.reject_with_redirect = reject_with_redirect
    client = EvcNetClient(session, USERNAME, "wrong", url)

    with pytest.raises(EvcNetAuthError) as exc_info:
        await client.charge_points()
    assert type(exc_info.value) is EvcNetAuthError


async def test_session_refused_after_login(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test accepted credentials with an unusable session do not ask for a password."""
    fake, url = portal
    fake.ajax_ignores_sessions = True
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    with pytest.raises(EvcNetConnectionError, match="accepted the login"):
        await client.charge_points()


async def test_html_answer_is_a_connection_error(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test that a maintenance page does not look like an auth failure."""
    fake, url = portal
    fake.html_ajax = True
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    with pytest.raises(EvcNetConnectionError):
        await client.charge_points()


async def test_cards(
    portal: tuple[FakePortal, str], session: aiohttp.ClientSession
) -> None:
    """Test reading the active cards from the Cards page."""
    _, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    assert await client.cards() == [
        Card(id="501", number="NL-ABC-C12345678-X"),
        Card(id="502", number="NL-XYZ-C87654321-Y"),
    ]


@pytest.mark.parametrize(
    ("action", "kwargs", "expected"),
    [
        pytest.param(
            ChargePointAction.HARD_RESET, {}, {"clickedButtonId": 1}, id="hard_reset"
        ),
        pytest.param(
            ChargePointAction.SOFT_RESET, {}, {"clickedButtonId": 1}, id="soft_reset"
        ),
        pytest.param(
            ChargePointAction.GET_STATUS, {}, {"clickedButtonId": 1}, id="get_status"
        ),
        pytest.param(
            ChargePointAction.BLOCK,
            {},
            {"clickedButtonId": 0, "channel": "0"},
            id="block_all_channels",
        ),
        pytest.param(
            ChargePointAction.UNBLOCK,
            {"channel": "1"},
            {"clickedButtonId": 0, "channel": "1"},
            id="unblock_channel",
        ),
        pytest.param(
            ChargePointAction.UNLOCK_CONNECTOR,
            {"channel": "1"},
            {"clickedButtonId": 0, "channel": "1"},
            id="unlock_connector",
        ),
        pytest.param(
            ChargePointAction.START_TRANSACTION,
            {"channel": "1", "card": "501"},
            {"clickedButtonId": 0, "channel": "1", "customer": None, "card": "501"},
            id="start_transaction",
        ),
        pytest.param(
            ChargePointAction.STOP_TRANSACTION,
            {"channel": "1"},
            {"clickedButtonId": 0, "channel": "1"},
            id="stop_transaction",
        ),
    ],
)
async def test_actions(
    portal: tuple[FakePortal, str],
    session: aiohttp.ClientSession,
    action: ChargePointAction,
    kwargs: dict[str, str],
    expected: dict[str, Any],
) -> None:
    """Test commands are sent like the portal's dashboard buttons send them."""
    fake, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    assert await client.action(CHARGE_POINT_ID, action, **kwargs) is True

    assert fake.actions == [
        {"action": action.value, "rechargeSpotId": CHARGE_POINT_ID, **expected}
    ]


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        pytest.param(
            ChargePointAction.START_TRANSACTION,
            {"channel": "1"},
            id="start_without_card",
        ),
        pytest.param(ChargePointAction.STOP_TRANSACTION, {}, id="stop_without_channel"),
        pytest.param(
            ChargePointAction.UNLOCK_CONNECTOR, {}, id="unlock_without_channel"
        ),
    ],
)
async def test_action_missing_arguments(
    portal: tuple[FakePortal, str],
    session: aiohttp.ClientSession,
    action: ChargePointAction,
    kwargs: dict[str, str],
) -> None:
    """Test commands that cannot be sent without more information."""
    fake, url = portal
    client = EvcNetClient(session, USERNAME, PASSWORD, url)

    with pytest.raises(ValueError):
        await client.action(CHARGE_POINT_ID, action, **kwargs)
    assert fake.actions == []


async def test_unreachable(session: aiohttp.ClientSession) -> None:
    """Test a portal that cannot be reached."""
    client = EvcNetClient(session, USERNAME, PASSWORD, "http://127.0.0.1:9")

    with pytest.raises(EvcNetConnectionError):
        await client.charge_points()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param("1000000000000000", ChannelState.OFFLINE, id="no_communication"),
        pytest.param("2000000000000000", ChannelState.OFFLINE, id="power_failure"),
        pytest.param("0000000000030000", ChannelState.BLOCKED, id="blocked"),
        pytest.param("0000000000000100", ChannelState.FAULTY, id="rcd_tripped"),
        pytest.param("4000000000000000", ChannelState.FAULTY, id="generic_faulty"),
        pytest.param("0000000000010001", ChannelState.CHARGING, id="charging"),
        pytest.param(
            "0000000000050000", ChannelState.CHARGING_COMPLETE, id="charging_complete"
        ),
        pytest.param("0000000000000400", ChannelState.RESERVED, id="reserved"),
        pytest.param("0000000000000010", ChannelState.AVAILABLE, id="lock_only"),
        pytest.param("0", ChannelState.AVAILABLE, id="short_zero"),
        pytest.param("0000000000000000", ChannelState.AVAILABLE, id="available"),
    ],
)
def test_channel_state(code: str, expected: ChannelState) -> None:
    """Test the state derivation mirrors the portal's status icon."""
    parsed = parse_status(code)
    assert parsed is not None
    assert channel_state(*parsed) is expected


@pytest.mark.parametrize("code", [None, "", "zz"])
def test_unparseable_status(code: str | None) -> None:
    """Test missing or broken status codes."""
    assert parse_status(code) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("532", 532.0),
        ("1,532", 1532.0),
        ("7,36", 7.36),
        ("1.234,5", 1234.5),
        ("1,234.5", 1234.5),
        (7.4, 7.4),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_number(value: Any, expected: float | None) -> None:
    """Test number parsing across locales."""
    assert _number(value) == expected
