"""Client-side imports stay light: every `j` command pays for them again."""

import subprocess
import sys

import pytest

from . import adapters


def _modules_loaded_by(module: str) -> set[str]:
    """Import a module in a fresh interpreter and return everything it loaded."""
    code = f"import sys, {module}; print('\\n'.join(sys.modules))"
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    return set(result.stdout.split())


def test_client_does_not_import_driver_side_code():
    loaded = _modules_loaded_by("jumpstarter_driver_network.client")
    assert {"jumpstarter_driver_network.driver", "jumpstarter.driver", "aiohttp"}.isdisjoint(loaded)


def test_adapters_are_imported_on_first_use():
    loaded = _modules_loaded_by("jumpstarter_driver_network.adapters")
    assert {"fabric", "paramiko", "websockets", "wsproto", "pexpect"}.isdisjoint(loaded)


@pytest.mark.parametrize("name", adapters.__all__)
def test_every_adapter_resolves(name):
    assert getattr(adapters, name).__name__ == name


def test_unknown_adapter_raises_attribute_error():
    with pytest.raises(AttributeError, match="NoSuchAdapter"):
        _ = adapters.NoSuchAdapter
