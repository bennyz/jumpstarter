"""Definitions shared by the network drivers and their clients.

These live apart from driver.py so a client can use them without importing
driver-side code, which pulls in aiohttp and the rest of jumpstarter.driver.
"""

DBUS_NETWORK_KIND_LABEL = "jumpstarter.dev/dbusnetwork/kind"
