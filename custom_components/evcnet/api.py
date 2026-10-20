"""Async client for the internal AJAX API of the EVC-net charging portal.

EVC-net (used by 50five and other resellers) has no public API. The web UI
talks to ``/api/ajax`` with a JSON-RPC-like ``requests`` query parameter and a
PHP session cookie obtained by posting the login form, optionally followed by
a code the portal emails to the user.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntFlag, StrEnum
from html.parser import HTMLParser
import json
import re
from typing import Any, Self

import aiohttp
from aiohttp import hdrs
from yarl import URL

DEFAULT_BASE_URL = "https://50five-snl.evc-net.com"

_RECHARGE_SPOTS = "\\LMS\\EV\\AsyncServices\\RechargeSpotsAsyncService"
_DASHBOARD = "\\LMS\\EV\\AsyncServices\\DashboardAsyncService"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_ALL_TRANSACTIONS = 100_000
_MAX_REDIRECTS = 5


class EvcNetError(Exception):
    """Base error for the EVC-net client."""


class EvcNetConnectionError(EvcNetError):
    """The portal could not be reached or answered unexpectedly."""


class EvcNetAuthError(EvcNetError):
    """The portal rejected the credentials."""


class EvcNetTwoFactorRequired(EvcNetAuthError):
    """The portal emailed a verification code; finish with ``verify_code``."""


class EvcNetInvalidCode(EvcNetAuthError):
    """The portal rejected the verification code."""


class EvcNetSessionExpired(EvcNetAuthError):
    """The session ended and logging in again needs the user."""


class ChannelFlag(IntFlag):
    """Low 32 bits of a channel status (``CHANNEL_STATUS_*`` in the portal JS)."""

    RELAY1_ACTIVE = 0x00000001
    RELAY2_ACTIVE = 0x00000002
    VENTILATION_REQUIRED = 0x00000004
    CABLE_UNDERRATED = 0x00000008
    LOCK_ENGAGED = 0x00000010
    WATCHDOG_RESET_OCCURRED = 0x00000020
    CP_DIODE_DETECTION_ERROR = 0x00000040
    SIMPLE_CHARGER_NETWORK_FOUND = 0x00000080
    RCD_TRIPPED = 0x00000100
    UNKNOWN_PP_VALUE = 0x00000200
    RESERVATION_ACTIVE = 0x00000400
    KWH_ERROR = 0x00000800
    RCD2_TRIPPED = 0x00001000
    RELAY_ERROR = 0x00002000
    FUSE_TRIPPED = 0x00004000
    CARD_EXPIRED = 0x00008000
    TRANSACTION = 0x00010000
    BLOCKED = 0x00020000
    CHARGING_COMPLETE = 0x00040000
    FOUND_LOW_COSPHI = 0x00080000
    STATE_CB_A_B_DETECTED = 0x00100000
    TEMPERATURE_WARNING = 0x00200000
    TEMPERATURE_LIMIT_REACHED = 0x00400000
    OVERCURRENT_ERROR = 0x00800000
    CP_STATE_C_DETECTED = 0x01000000
    ERROR = 0x02000000
    PREPAID_LIMIT_REACHED = 0x04000000
    UTILITY_KWH_ERROR = 0x08000000
    TAG_READER_ERROR = 0x10000000
    CP_UNKNOWN = 0x20000000
    ALARM_INPUT_ACTIVATED = 0x40000000
    EVENTLOG_FULL = 0x80000000


class ChannelFlag2(IntFlag):
    """High 32 bits of a channel status (``CHANNEL_STATUS2_*`` in the portal JS)."""

    EMERGENCY_SWITCH = 0x00000001
    DCC_VEHICLE_RELATED_ERROR = 0x00000002
    DCC_POWER_SUPPLY_RELATED_ERROR = 0x00000004
    DCC_ISOLATION_ERROR = 0x00000008
    CHARGING_PROFILE_ACTIVE = 0x00000010
    CP_STATE_CHANGED_TOO_OFTEN = 0x00000020
    NO_COMMUNICATION = 0x10000000
    POWER_FAILURE = 0x20000000
    GENERIC_FAULTY = 0x40000000


class ChannelState(StrEnum):
    """Summary state, matching the coloured status dot in the portal."""

    AVAILABLE = "available"
    CHARGING = "charging"
    CHARGING_COMPLETE = "charging_complete"
    RESERVED = "reserved"
    BLOCKED = "blocked"
    FAULTY = "faulty"
    OFFLINE = "offline"


class ChargePointAction(StrEnum):
    """Commands behind the buttons of the portal's real-time dashboard."""

    HARD_RESET = "HardReset"
    SOFT_RESET = "SoftReset"
    BLOCK = "Block"
    UNBLOCK = "Unblock"
    UNLOCK_CONNECTOR = "UnlockConnector"
    START_TRANSACTION = "StartTransaction"
    STOP_TRANSACTION = "StopTransaction"
    GET_STATUS = "GetStatus"


# The portal asks Yes/No for these and OK/Cancel for the rest; the answer is
# sent along as clickedButtonId (ModalMessageBox.RESULT_YES / RESULT_OK).
_YES_NO_ACTIONS = {
    ChargePointAction.HARD_RESET,
    ChargePointAction.SOFT_RESET,
    ChargePointAction.GET_STATUS,
}
_RESULT_OK = 0
_RESULT_YES = 1
# Channel "0" means all channels in the portal's dialog.
_ALL_CHANNELS = "0"
_CHANNEL_ACTIONS = {
    ChargePointAction.BLOCK,
    ChargePointAction.UNBLOCK,
    ChargePointAction.UNLOCK_CONNECTOR,
    ChargePointAction.START_TRANSACTION,
    ChargePointAction.STOP_TRANSACTION,
}

# Masks copied from ChannelStatusIcon.setStatus in the portal JS.
_OFFLINE_MASK2 = ChannelFlag2.NO_COMMUNICATION | ChannelFlag2.POWER_FAILURE
_FAULTY_MASK2 = 0x4000002F
_FAULTY_MASK = 0xD8407940

_THOUSANDS_COMMA = re.compile(r"\d{1,3}(,\d{3})+")
_HOURS_MINUTES = re.compile(r"\s*(\d+)\s*[:hHuU]\s*(\d{1,2})")


def parse_status(code: str | None) -> tuple[ChannelFlag2, ChannelFlag] | None:
    """Split the 16 hex digit status string into its two flag words."""
    if not code:
        return None
    padded = str(code).rjust(16, "0")
    try:
        return ChannelFlag2(int(padded[:8], 16)), ChannelFlag(int(padded[8:], 16))
    except ValueError:
        return None


def channel_state(flags2: ChannelFlag2, flags: ChannelFlag) -> ChannelState:
    """Derive the summary state the same way the portal colours its icon."""
    if flags2 & _OFFLINE_MASK2:
        return ChannelState.OFFLINE
    if flags & ChannelFlag.BLOCKED:
        return ChannelState.BLOCKED
    if flags2 & _FAULTY_MASK2 or flags & _FAULTY_MASK:
        return ChannelState.FAULTY
    if flags & ChannelFlag.TRANSACTION:
        if flags & ChannelFlag.CHARGING_COMPLETE:
            return ChannelState.CHARGING_COMPLETE
        return ChannelState.CHARGING
    if flags & ChannelFlag.RESERVATION_ACTIVE:
        return ChannelState.RESERVED
    return ChannelState.AVAILABLE


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = (
            text.replace(",", "")
            if _THOUSANDS_COMMA.fullmatch(text)
            else text.replace(",", ".")
        )
    try:
        return float(text)
    except ValueError:
        return None


def _minutes(value: Any) -> int | None:
    if not value or not (match := _HOURS_MINUTES.match(str(value))):
        return None
    return int(match[1]) * 60 + int(match[2])


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


@dataclass(frozen=True, kw_only=True)
class ChargePoint:
    """Static information about a charge point on the account."""

    id: str
    name: str | None
    address: str | None
    software_version: str | None
    channels: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Channel:
    """One row of the "Real-time dashboard" table."""

    channel: str
    connector: str | None
    status_code: str | None
    state: ChannelState | None
    flags: tuple[str, ...]
    notification: str | None
    power_kw: float | None
    session_energy_kwh: float | None
    session_minutes: int | None
    state_of_charge: float | None
    card_id: str | None
    customer_name: str | None

    @property
    def online(self) -> bool | None:
        """Return whether the portal can talk to this channel."""
        if self.state is None:
            return None
        return self.state is not ChannelState.OFFLINE

    @property
    def in_transaction(self) -> bool:
        """Return whether a charging session is open on this channel."""
        return ChannelFlag.TRANSACTION.name.lower() in self.flags

    @classmethod
    def from_overview(cls, row: dict[str, Any]) -> Self:
        """Build from a ``RechargeSpotsAsyncService.overview`` row."""
        status_code = _text(row.get("STATUS"))
        state = None
        flags: list[str] = []
        if parsed := parse_status(status_code):
            flags2, flags1 = parsed
            state = channel_state(flags2, flags1)
            flags = [f.name.lower() for f in (*flags2, *flags1) if f.name]
        return cls(
            channel=str(row.get("CHANNEL", "1")),
            connector=_text(row.get("CONNECTOR")),
            status_code=status_code,
            state=state,
            flags=tuple(flags),
            notification=_text(row.get("NOTIFICATION")),
            power_kw=_number(row.get("MOM_POWER_KW")),
            session_energy_kwh=_number(row.get("TRANS_ENERGY_DELIVERED_KWH")),
            session_minutes=_minutes(row.get("TRANSACTION_TIME_H_M")),
            state_of_charge=_number(row.get("SOC")),
            card_id=_text(row.get("CARDID")),
            customer_name=_text(row.get("CUSTOMER_NAME")),
        )


@dataclass(frozen=True, kw_only=True)
class ChargePointStatus:
    """Live status of a charge point."""

    channels: dict[str, Channel]
    total_energy_kwh: float | None = None

    @property
    def online(self) -> bool | None:
        """Return True if at least one channel is reachable."""
        states = [ch.online for ch in self.channels.values() if ch.online is not None]
        if not states:
            return None
        return any(states)


def _request(handler: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"handler": handler, "method": method, "params": params}


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True, kw_only=True)
class Transaction:
    """A charging session from the portal's transaction history."""

    id: str
    channel: str | None
    start: datetime | None
    end: datetime | None
    energy_kwh: float | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        """Build from a ``DashboardAsyncService.transactions`` row."""
        return cls(
            id=str(row.get("IDX")),
            channel=_text(row.get("CHANNEL")),
            start=_timestamp(row.get("EVENTDATE_ISO")),
            end=_timestamp(row.get("ENDDATE_ISO")),
            energy_kwh=_number(row.get("ENERGY_CONSUMED_KWH")),
        )


@dataclass(frozen=True, kw_only=True)
class Card:
    """A charging card on the account."""

    id: str
    number: str


_CARD_LINK = re.compile(r"/Cards/DeactivateCard/(\d+)")


class _TableParser(HTMLParser):
    """Collect the cell texts and links of every table body row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[list[str], list[str]]] = []
        self._in_body = False
        self._row: tuple[list[str], list[str]] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._in_body:
            self._row = ([], [])
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag == "a" and self._row is not None and (href := dict(attrs).get("href")):
            self._row[1].append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row[0].append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "tbody":
            self._in_body = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None and (text := data.strip()):
            self._cell.append(text)


def _parse_cards(html: str) -> list[Card]:
    """Read the active cards from the portal's Cards page."""
    parser = _TableParser()
    parser.feed(html)
    cards = []
    for cells, links in parser.rows:
        # Only active cards have a deactivate link; it carries the internal id.
        card_id = next((m[1] for link in links if (m := _CARD_LINK.search(link))), None)
        if card_id and cells and cells[0]:
            cards.append(Card(id=card_id, number=cells[0]))
    return cards


_MASKED_EMAIL = re.compile(r"[\w.*+-]+@[\w.*-]+\.[\w*]+")


@dataclass
class _Form:
    action: str
    fields: list[dict[str, str]] = field(default_factory=list)


class _FormParser(HTMLParser):
    """Collect the forms and the visible text of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[_Form] = []
        self.text: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag in ("script", "style"):
            self._hidden_depth += 1
        elif tag == "form":
            self.forms.append(_Form(action=attributes.get("action", "")))
        elif tag in ("input", "button") and self.forms:
            self.forms[-1].fields.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and (text := data.strip()):
            self.text.append(text)


@dataclass(frozen=True, kw_only=True)
class _TwoFactorForm:
    action: URL
    hidden: dict[str, str]
    code_field: str
    submit: tuple[str, str] | None
    destination: str | None


def _parse_two_factor(html: str, page_url: URL) -> _TwoFactorForm | None:
    """Find the verification code form on the portal's 2FA page."""
    parser = _FormParser()
    parser.feed(html)
    for form in parser.forms:
        code_field = next(
            (
                f["name"]
                for f in form.fields
                if f.get("name")
                and (
                    f.get("autocomplete") == "one-time-code" or "auth_code" in f["name"]
                )
            ),
            None,
        )
        if code_field is None:
            continue
        return _TwoFactorForm(
            action=page_url.join(URL(form.action)) if form.action else page_url,
            hidden={
                f["name"]: f.get("value", "")
                for f in form.fields
                if f.get("type") == "hidden" and f.get("name")
            },
            code_field=code_field,
            submit=next(
                (
                    (f["name"], f.get("value", ""))
                    for f in form.fields
                    if f.get("type") == "submit" and f.get("name")
                ),
                None,
            ),
            destination=next(
                (m[0] for text in parser.text if (m := _MASKED_EMAIL.search(text))),
                None,
            ),
        )
    return None


def _is_login_page(url: URL) -> bool:
    return url.path.lower().startswith("/login")


def _is_two_factor_page(url: URL) -> bool:
    return url.path.lower().startswith("/2fa")


class EvcNetClient:
    """Session-based client for an EVC-net portal.

    The client keeps the session cookies itself so they can be saved and
    restored; give it a session with ``aiohttp.DummyCookieJar``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cookies: Mapping[str, str] | None = None,
        auto_login: bool = True,
    ) -> None:
        """Initialize the client.

        With ``auto_login`` disabled an expired session raises
        EvcNetSessionExpired instead of logging in again, so a portal using
        email verification does not send codes nobody asked for.
        """
        self._session = session
        self._username = username
        self._password = password
        self._base = URL(base_url.rstrip("/"))
        self._cookies: dict[str, str] = dict(cookies or {})
        self._auto_login = auto_login
        self._two_factor: _TwoFactorForm | None = None

    @property
    def cookies(self) -> dict[str, str]:
        """Return the current session cookies."""
        return dict(self._cookies)

    @property
    def two_factor_destination(self) -> str | None:
        """Return the masked address the pending verification code went to."""
        return self._two_factor.destination if self._two_factor else None

    async def login(self) -> None:
        """Log in the way a browser does.

        Raises EvcNetTwoFactorRequired when the portal emailed a code.
        """
        self._cookies.clear()
        self._two_factor = None
        await self._request("GET", self._url("Login/Login"))
        form = aiohttp.FormData(
            {
                "emailField": self._username,
                "passwordField": self._password,
                "Login": "Log in",
            },
            default_to_multipart=True,
        )
        response = await self._request("POST", self._url("Login/Login"), data=form)
        await self._finish_login(await self._follow_redirects(response))

    async def verify_code(self, code: str) -> None:
        """Submit the emailed verification code of a pending login."""
        if (pending := self._two_factor) is None:
            raise EvcNetAuthError("No verification is pending; log in first")
        fields = {**pending.hidden, pending.code_field: code.strip()}
        if pending.submit:
            fields[pending.submit[0]] = pending.submit[1]
        response = await self._request(
            "POST",
            pending.action,
            data=aiohttp.FormData(fields, default_to_multipart=True),
        )
        landing = await self._follow_redirects(response)
        if _is_two_factor_page(landing.url):
            self._two_factor = _parse_two_factor(await landing.text(), landing.url)
            if self._two_factor is None:
                raise EvcNetConnectionError("Unrecognised verification page")
            raise EvcNetInvalidCode("The portal rejected the verification code")
        if _is_login_page(landing.url):
            self._two_factor = None
            raise EvcNetAuthError("The verification session ended; log in again")
        await self._finish_login(landing)

    async def charge_points(self) -> list[ChargePoint]:
        """Return all charge points on the account."""
        (rows,) = await self._call(_network_overview())
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("IDX") is not None:
                grouped.setdefault(str(row["IDX"]), []).append(row)
        return [
            ChargePoint(
                id=cp_id,
                name=_text(cp_rows[0].get("NAME")),
                address=_text(cp_rows[0].get("ADDRESS")),
                software_version=_text(cp_rows[0].get("SOFTWARE_VERSION")),
                channels=tuple(str(r.get("CHANNEL", "1")) for r in cp_rows),
            )
            for cp_id, cp_rows in grouped.items()
        ]

    async def status(
        self, charge_point_ids: Iterable[str]
    ) -> dict[str, ChargePointStatus]:
        """Return the real-time dashboard data for the given charge points."""
        ids = list(charge_point_ids)
        if not ids:
            return {}
        results = await self._call(
            *(
                _request(_RECHARGE_SPOTS, "overview", {"rechargeSpotId": cp_id})
                for cp_id in ids
            )
        )
        statuses: dict[str, ChargePointStatus] = {}
        for cp_id, rows in zip(ids, results, strict=True):
            channels = [
                Channel.from_overview(row)
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ]
            statuses[cp_id] = ChargePointStatus(
                channels={ch.channel: ch for ch in channels}
            )
        return statuses

    async def transactions(
        self, charge_point_ids: Iterable[str]
    ) -> dict[str, list[Transaction]]:
        """Return the full charging history of the given charge points."""
        ids = list(charge_point_ids)
        if not ids:
            return {}
        results = await self._call(
            *(
                _request(
                    _DASHBOARD,
                    "transactions",
                    {
                        "rechargeSpotIds": [cp_id],
                        "customerId": None,
                        "cardId": None,
                        "transactions": _ALL_TRANSACTIONS,
                        "transactionsMaxAge": None,
                        "mode": None,
                        "maxCache": 0,
                    },
                )
                for cp_id in ids
            )
        )
        return {
            cp_id: [
                Transaction.from_row(row)
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ]
            for cp_id, rows in zip(ids, results, strict=True)
        }

    async def cards(self) -> list[Card]:
        """Return the account's active charging cards."""
        # Makes sure the session works (or fails the usual way) first.
        await self._call(_network_overview())
        response = await self._request("GET", self._url("Cards/List"))
        page = await self._follow_redirects(response)
        if _is_login_page(page.url) or _is_two_factor_page(page.url):
            raise EvcNetSessionExpired("The portal session has ended")
        return _parse_cards(await page.text())

    async def action(
        self,
        charge_point_id: str,
        action: ChargePointAction,
        *,
        channel: str | None = None,
        card: str | None = None,
    ) -> Any:
        """Send a command the way the portal's dashboard buttons do.

        ``channel`` defaults to all channels where the portal allows that;
        unlocking and stopping need a specific channel, starting needs the
        internal id of a card.
        """
        args: dict[str, Any] = {
            "action": action.value,
            "rechargeSpotId": charge_point_id,
            "clickedButtonId": (
                _RESULT_YES if action in _YES_NO_ACTIONS else _RESULT_OK
            ),
        }
        if action in _CHANNEL_ACTIONS:
            if channel is None and action in (
                ChargePointAction.UNLOCK_CONNECTOR,
                ChargePointAction.STOP_TRANSACTION,
            ):
                raise ValueError(f"{action} needs a channel")
            args["channel"] = channel or _ALL_CHANNELS
        if action is ChargePointAction.START_TRANSACTION:
            if not card:
                raise ValueError("Starting a transaction needs a card")
            args["customer"] = None
            args["card"] = card
        (result,) = await self._call(_request(_RECHARGE_SPOTS, "action", args))
        return result

    async def _finish_login(self, landing: aiohttp.ClientResponse) -> None:
        if _is_login_page(landing.url):
            raise EvcNetAuthError("The portal rejected the username or password")
        if _is_two_factor_page(landing.url):
            self._two_factor = _parse_two_factor(await landing.text(), landing.url)
            if self._two_factor is None:
                raise EvcNetConnectionError("Unrecognised verification page")
            raise EvcNetTwoFactorRequired("The portal emailed a verification code")
        self._two_factor = None
        if not await self._ajax(_network_overview()):
            raise EvcNetConnectionError(
                "The portal accepted the login but rejected the session"
            )

    async def _call(self, *requests: dict[str, Any]) -> list[Any]:
        # An expired session gets an empty list, not a 401.
        if self._cookies:
            if result := await self._ajax(*requests):
                return self._check_length(result, requests)
            await self._recover_session()
        elif self._auto_login:
            await self.login()
        else:
            raise EvcNetSessionExpired("There is no portal session")
        if not (result := await self._ajax(*requests)):
            raise EvcNetConnectionError("The portal keeps answering empty")
        return self._check_length(result, requests)

    async def _recover_session(self) -> None:
        """Find out why the API answered empty and log in again if allowed."""
        response = await self._request("GET", self._url("Overview"))
        landing = await self._follow_redirects(response)
        if _is_two_factor_page(landing.url):
            raise EvcNetSessionExpired("The portal asks for a new verification code")
        if not _is_login_page(landing.url):
            return
        if not self._auto_login:
            raise EvcNetSessionExpired("The portal session has ended")
        await self.login()

    @staticmethod
    def _check_length(result: list[Any], requests: tuple[Any, ...]) -> list[Any]:
        if len(result) != len(requests):
            raise EvcNetConnectionError(
                f"Expected {len(requests)} results from the portal, got {len(result)}"
            )
        return result

    async def _follow_redirects(
        self, response: aiohttp.ClientResponse
    ) -> aiohttp.ClientResponse:
        """Follow redirects like a browser would and return the final response."""
        for _ in range(_MAX_REDIRECTS):
            location = response.headers.get(hdrs.LOCATION)
            if not 300 <= response.status < 400 or not location:
                return response
            target = response.url.join(URL(location))
            if target.host != self._base.host:
                return response
            # The portal redirects to plain http and relies on HSTS. Following
            # that literally drops the secure session cookie and the portal
            # answers with a new, anonymous session.
            response = await self._request("GET", target.with_scheme(self._base.scheme))
        raise EvcNetConnectionError("The portal redirected too many times")

    async def _ajax(self, *requests: dict[str, Any]) -> list[Any]:
        payload = json.dumps({str(i): req for i, req in enumerate(requests)})
        response = await self._request(
            "GET", self._url("api/ajax"), params={"requests": payload}
        )
        if 300 <= response.status < 400:
            return []
        try:
            result = json.loads(await response.text())
        except ValueError as err:
            raise EvcNetConnectionError("The portal returned invalid JSON") from err
        if not isinstance(result, list):
            raise EvcNetConnectionError("The portal returned an unexpected payload")
        return result

    def _url(self, path: str) -> URL:
        return self._base / path

    async def _request(
        self, method: str, url: URL, **kwargs: Any
    ) -> aiohttp.ClientResponse:
        """Send one request without following redirects; the body is preloaded."""
        headers = dict(kwargs.pop("headers", None) or {})
        if self._cookies:
            headers[hdrs.COOKIE] = "; ".join(
                f"{name}={value}" for name, value in self._cookies.items()
            )
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                timeout=_TIMEOUT,
                allow_redirects=False,
                **kwargs,
            ) as response:
                self._store_cookies(response)
                if response.status >= 400:
                    raise EvcNetConnectionError(
                        f"{method} {url.path} returned HTTP {response.status}"
                    )
                await response.read()
                return response
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EvcNetConnectionError(f"Error talking to the portal: {err}") from err

    def _store_cookies(self, response: aiohttp.ClientResponse) -> None:
        for name, morsel in response.cookies.items():
            if morsel.value in ("", "deleted") or morsel["max-age"] == "0":
                self._cookies.pop(name, None)
            else:
                self._cookies[name] = morsel.value


def _network_overview() -> dict[str, Any]:
    return _request(_DASHBOARD, "networkOverview", {"mode": "id"})
