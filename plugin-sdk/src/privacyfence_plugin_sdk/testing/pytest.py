"""The ``plugin_host`` pytest fixture.

Load it from a ``conftest.py``::

    pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"]

and ask for it in a test. The fixture is a factory that builds a :class:`PluginTestHost` with the
same arguments, to use as an async context manager::

    async def test_home_page(plugin_host):
        async with plugin_host(plugin) as host:
            response = await host.get("/")
            assert response.status == 200

This module does not import ``pytest`` until pytest asks for the fixture, so importing it costs
nothing for a project that does not use pytest.
"""
from __future__ import annotations

from typing import Any


def _make_fixture() -> Any:
    import pytest

    from ._host import PluginTestHost

    @pytest.fixture
    def plugin_host() -> Any:
        return PluginTestHost

    return plugin_host


def __getattr__(name: str) -> Any:
    if name == "plugin_host":
        fixture = _make_fixture()
        globals()["plugin_host"] = fixture
        return fixture
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), "plugin_host"})
