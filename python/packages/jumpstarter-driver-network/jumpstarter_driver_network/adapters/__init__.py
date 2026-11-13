"""Client-side adapters for network drivers.

Each adapter is imported the first time it is used. Importing them all up front
made every client that needs one adapter (ssh, serial, adb, ...) pay for
fabric, paramiko and the noVNC websocket stack on every `j` command.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dbus import DbusAdapter
    from .fabric import FabricAdapter
    from .novnc import NovncAdapter
    from .pexpect import PexpectAdapter
    from .portforward import TcpPortforwardAdapter, UnixPortforwardAdapter

_MODULES = {
    "DbusAdapter": ".dbus",
    "FabricAdapter": ".fabric",
    "NovncAdapter": ".novnc",
    "PexpectAdapter": ".pexpect",
    "TcpPortforwardAdapter": ".portforward",
    "UnixPortforwardAdapter": ".portforward",
}

__all__ = [
    "DbusAdapter",
    "FabricAdapter",
    "NovncAdapter",
    "PexpectAdapter",
    "TcpPortforwardAdapter",
    "UnixPortforwardAdapter",
]


def __getattr__(name: str):
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
