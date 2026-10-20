"""Constants for the EVC-net integration."""

from datetime import timedelta
import logging

DOMAIN = "evcnet"
LOGGER = logging.getLogger(__package__)

# The portal's own dashboard refreshes every 15 seconds.
SCAN_INTERVAL = timedelta(seconds=30)
# The charging history (~0.5 kB per session) is also re-read when a session
# starts or ends; this catches sessions the portal lists late.
TRANSACTIONS_REFRESH_INTERVAL = timedelta(minutes=10)

CONF_CODE = "code"
CONF_COOKIES = "cookies"
CONF_TWO_FACTOR = "two_factor"
