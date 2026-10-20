"""Constants for the EVC-net integration."""

from datetime import timedelta
import logging

DOMAIN = "evcnet"
LOGGER = logging.getLogger(__package__)

# The portal's own dashboard refreshes every 15 seconds.
SCAN_INTERVAL = timedelta(seconds=30)

CONF_CODE = "code"
CONF_COOKIES = "cookies"
CONF_TWO_FACTOR = "two_factor"
