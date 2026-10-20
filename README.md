# EVC-net (50five) for Home Assistant

> [!WARNING]
> **This integration was completely vibe coded and is not actively maintained.**
>
> It was built in one sitting by an AI coding assistant, by reverse engineering
> the 50five / EVC-net web portal, and tested against a single home charger.
> There is no official API behind it. The portal can change at any moment and
> break it, and nobody is committed to fixing it when that happens. Issues and
> pull requests may go unanswered. Use it at your own risk, and feel free to
> fork it.
>
> It is not affiliated with, endorsed by, or supported by 50five, Threeforce
> or EVC-net.

Unofficial custom integration that brings the **Real-time dashboard** of an
EVC-net charging portal (such as `https://50five-snl.evc-net.com`) into Home
Assistant: whether the charger is online, its status, power, session energy,
session time, the RFID card in use, and the lifetime energy delivered.

It logs in the same way the website does and reads the portal's internal JSON
endpoint. It only reads data and never presses any of the portal's buttons
(reset, block, start/stop, and so on).

## Requirements

- Home Assistant 2025.2 or newer.
- An account on an EVC-net based portal, such as
  `https://50five-snl.evc-net.com`, with at least one charge point.
- Access to the mailbox of that account if it uses email verification (2FA).

## Installation

### With HACS

1. In Home Assistant, open **HACS**.
2. Open the menu (⋮) in the top right and choose **Custom repositories**.
3. Add `https://github.com/bgoncal/ha-evcnet` with type **Integration**.
4. Search for **EVC-net (50five)**, open it and choose **Download**.
5. Restart Home Assistant.

### Manually

1. Download this repository (**Code → Download ZIP**) and unpack it.
2. Copy the `custom_components/evcnet` folder into the `custom_components`
   folder of your Home Assistant configuration, creating it if needed, so that
   you end up with `config/custom_components/evcnet/manifest.json`.
3. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration** and search for
   **EVC-net (50five)**.
2. Fill in:
   - **Portal URL**: the site you log in to. The default is
     `https://50five-snl.evc-net.com`. Change it if your provider uses another
     EVC-net address.
   - **Username**: usually your email address.
   - **Password**: your portal password.
3. If your account uses email verification, the portal emails a 6-digit code
   and Home Assistant shows a **Verification code** step with the (masked)
   address it went to. Enter the code. If you mistype it, you can try again
   without getting a new email.
4. Every charge point on the account is added as a device.

### Staying logged in

The portal has no "remember this device" option, so every new login with email
verification sends a code. To avoid that, Home Assistant saves the portal
session and keeps it alive by polling, the same way an open browser tab stays
logged in. Restarts reuse the saved session.

If the session ends anyway (Home Assistant off for about a day, or the portal
resets sessions), the integration does **not** log in on its own, because that
would email you a code you didn't ask for. It shows a **Re-authenticate**
prompt under **Settings → Devices & services** instead:

1. Open the prompt and submit it. Leave the password empty to keep the saved
   one, or enter a new one if you changed it.
2. Enter the code from the new email.

Accounts without email verification log in again silently.

### Checking the login without Home Assistant

```bash
uv run --python 3.13 --with aiohttp scripts/smoke_test.py
```

This prompts for your credentials (and the emailed code, if needed) and prints
exactly what the integration would see.

## Entities

Each charge point becomes a device. A charge point with more than one connector
gets an extra device per connector.

| Entity | Source column | Notes |
| --- | --- | --- |
| `binary_sensor.…_connectivity` | Status | Off when the portal shows *No communication* or *Power failure* |
| `sensor.…_status` | Status dot | `available`, `charging`, `charging_complete`, `reserved`, `blocked`, `faulty`, `offline`. Attributes: decoded `flags` and the raw `status_code` |
| `sensor.…_notification` | Notification | The portal's text, e.g. "No communication" |
| `sensor.…_power` | Power (kW) | 0 while idle and online; unknown while offline |
| `sensor.…_session_energy` | Energy (kWh) | Energy of the current transaction |
| `sensor.…_session_duration` | Transaction time | In minutes |
| `sensor.…_card` | Card | Attribute `customer` holds the Client column |
| `binary_sensor.…_charging` | Status | On during an active transaction that hasn't completed |
| `binary_sensor.…_problem` | Status | On when the portal would show the red "Faulty" dot |
| `sensor.…_total_energy_delivered` | Transaction history + live session | Lifetime kWh with 0.01 kWh resolution; **use this one in the Energy dashboard** |
| `sensor.…_state_of_charge` | SoC | Disabled by default; AC chargers rarely report it |
| `sensor.…_connector` | Connector type | Disabled by default, diagnostic |

The integration polls every 30 seconds (the portal's own page polls every 15).

## Energy dashboard

**Total energy delivered** is a lifetime energy meter (`total_increasing`, kWh),
so the Energy dashboard can use it for your charger's consumption:

1. Go to **Settings → Dashboards → Energy**.
2. Under **Individual devices**, choose **Add device** and pick
   *<your charger> Total energy delivered*.
3. Optionally pick the charger's **Power** sensor as its power source, if your
   Home Assistant version offers that, to see live charging power.

Don't also add **Session energy**: it covers the same energy one session at a
time, so the charger would be counted twice. It's still useful on its own
dashboards.

How the total is calculated:

- **Value:** the sum of every completed session in the portal's transaction
  history (to 0.01 kWh) plus the energy of the session in progress. It rises
  every 30 seconds while charging instead of jumping at the end of a session.
  The portal's own "Delivered" tile is the same sum rounded to whole kWh and
  cached for up to an hour, which is why it isn't used.
- **When the history is read:** when a session starts or ends, and every 10
  minutes. That's about 0.5 kB per session ever recorded, so the 30-second
  polls stay light.
- **Never decreasing:** a session that just ended may not be in the history
  yet. The value is held rather than dropping, since a drop would show up as
  negative energy, and catches up when the portal lists the session.
- **Missed energy:** if Home Assistant is off while you charge, nothing is
  lost. The next history read adds the session at its final value, so the
  dashboard puts all of that energy in the hour Home Assistant comes back.

## How it works

Everything goes through one endpoint:

```
GET /api/ajax?requests=<json>
```

`requests` is an object of numbered calls, and the response is a JSON array with
one result per call, in the same order:

```json
{
  "0": {
    "handler": "\\LMS\\EV\\AsyncServices\\RechargeSpotsAsyncService",
    "method": "overview",
    "params": { "rechargeSpotId": "<charge point id>" }
  }
}
```

| Handler | Method | Params | Returns |
| --- | --- | --- | --- |
| `DashboardAsyncService` | `networkOverview` | `{"mode": "id"}` | All charge points on the account: id, address, firmware, channels |
| `RechargeSpotsAsyncService` | `overview` | `{"rechargeSpotId": id}` | The real-time dashboard rows |
| `DashboardAsyncService` | `transactions` | `{"rechargeSpotIds": [id], "transactions": <max count>, ...}` | Charging history: start/end (ISO), `ENERGY_CONSUMED_KWH` with two decimals, price |
| `DashboardAsyncService` | `totalUsage` | `{"mode": "rechargeSpot", "rechargeSpotIds": [id], "maxCache": 3600}` | `{"number": "532", "unit": "kWh"}`, the rounded history total (not used) |
| `DashboardAsyncService` | `occupancy` / `current` / `spotsStatus` | see the portal | Occupancy chart, 24 h power chart, map status (not used) |

**Login.**

1. `GET /Login/Login` sets the `PHPSESSID` and `SERVERID` cookies.
2. `POST /Login/Login` sends a multipart form with `emailField`, `passwordField`
   and `Login=Log in`. On success it issues a new `PHPSESSID` and redirects
   to `/Overview`. On failure it ends back on `/Login`.
3. The redirect points at plain `http://`. Browsers switch to `https://` before
   connecting because the site sends HSTS. A client that follows the `http://`
   URL literally doesn't send the secure session cookie, so the portal hands
   out a new, anonymous session. The client therefore follows each redirect
   itself, switching to `https://`.
4. The session works only after `/Overview` has been loaded. With email
   verification, `/Overview` redirects to `/2fa` instead.
5. `/2fa` holds a form that posts to `/2fa_check` with a `_token`, the 6-digit
   `_auth_code` and `VerifyOtp=Verify`. The 2FA library is scheb/2fa-bundle,
   with its trusted-device feature disabled. A wrong code leads back to `/2fa`.

**Sessions.** `PHPSESSID` has a 24-hour `Max-Age` that is extended whenever it
is used. When a session is missing or has expired, `/api/ajax` replies
`200 []` rather than an HTTP error. On an empty answer the client loads
`/Overview`:

- If the portal sends it to `/Login`, the session has ended.
- If the page loads normally, the empty answer was a one-off and the call is
  retried.

The client sends its cookies itself and doesn't use aiohttp's cookie jar, so
they can be saved in the config entry and don't expire client-side.

**Status.** `STATUS` is 16 hex digits, read as two 32-bit flag words:

- **First 8 digits:** `0x10000000` means no communication, `0x20000000` power
  failure, and `0x40000000` generic fault. The remaining low bits carry DC and
  emergency-switch faults.
- **Last 8 digits:** `0x00010000` means a transaction is active, `0x00020000`
  blocked, `0x00040000` charging complete, and `0x00000400` reserved. The other
  bits are relay, lock, RCD, fuse, temperature and similar flags.

The summary state uses the same masks as the portal's `ChannelStatusIcon`. For
example, `1000000000000000` decodes to *No communication*. The full flag list is
in [`api.py`](custom_components/evcnet/api.py).

**Portal actions.** The dashboard buttons all call `RechargeSpotsAsyncService.action`
with `{"action": ..., "rechargeSpotId": ..., "channel": ...}`. The actions are
`HardReset`, `SoftReset`, `Block`, `Unblock`, `UnlockConnector`,
`StartTransaction`, `StopTransaction` and `GetStatus`. This integration is
read-only and never calls them.

## Known gaps

- The portal formats for power, energy and transaction time *while charging*
  were inferred from the field names and the portal's JS, not observed live.
  If a value looks wrong, download diagnostics from the device page (card IDs,
  addresses, credentials and session cookies are redacted) and compare it with
  the `status_code` attribute.
- Only tested with one single-connector home charger on the 50five portal.
- Read-only: there are no buttons or switches for the portal's actions.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv --only-binary cryptography -r requirements_test.txt
.venv/bin/pytest
```
