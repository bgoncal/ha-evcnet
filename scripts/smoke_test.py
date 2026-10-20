"""Log in to an EVC-net portal and print what the integration would see.

Runs the integration's API client without Home Assistant:

    uv run --python 3.13 --with aiohttp scripts/smoke_test.py

Credentials are read from EVCNET_USERNAME / EVCNET_PASSWORD or prompted for,
and so is the emailed verification code if the account uses one.
Set EVCNET_URL for portals other than 50five-snl.evc-net.com.
"""

import asyncio
from dataclasses import asdict
import getpass
import importlib.util
import json
import os
from pathlib import Path
import sys

import aiohttp

_API_PATH = Path(__file__).resolve().parents[1] / "custom_components/evcnet/api.py"
_spec = importlib.util.spec_from_file_location("evcnet_api", _API_PATH)
assert _spec and _spec.loader
api = importlib.util.module_from_spec(_spec)
sys.modules["evcnet_api"] = api
_spec.loader.exec_module(api)


async def main(username: str, password: str, base_url: str) -> None:
    """Print the charge points and their live status."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = api.EvcNetClient(session, username, password, base_url)
        try:
            await client.login()
        except api.EvcNetTwoFactorRequired:
            prompt = f"Code emailed to {client.two_factor_destination} (hidden): "
            await client.verify_code(await asyncio.to_thread(getpass.getpass, prompt))
        charge_points = await client.charge_points()
        statuses = await client.status(cp.id for cp in charge_points)

    print(
        json.dumps(
            {
                "charge_points": [asdict(cp) for cp in charge_points],
                "status": {cp_id: asdict(st) for cp_id, st in statuses.items()},
            },
            indent=2,
            default=str,
        )
    )
    for cp_id, status in statuses.items():
        for channel in status.channels.values():
            print(
                f"{cp_id} channel {channel.channel}: {channel.state} "
                f"(online={channel.online}, {channel.notification!r}), "
                f"total {status.total_energy_kwh} kWh"
            )


if __name__ == "__main__":
    asyncio.run(
        main(
            os.environ.get("EVCNET_USERNAME") or input("Username: "),
            os.environ.get("EVCNET_PASSWORD") or getpass.getpass(),
            os.environ.get("EVCNET_URL", api.DEFAULT_BASE_URL),
        )
    )
