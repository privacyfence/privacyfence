"""Tests for app_credentials.py: baked-in build credentials vs. dev env-var
fallback for Telegram's api_id/api_hash.

The generated `_telegram_credentials` module is git-ignored and, in this
dev checkout, may or may not actually exist on disk -- both branches are
forced deterministically via sys.modules rather than relying on that
ambient state.
"""
from __future__ import annotations

import sys
import types

import pytest

from privacyfence import app_credentials

MODULE_NAME = "privacyfence._telegram_credentials"


@pytest.fixture(autouse=True)
def _clean_module_cache(monkeypatch):
    # Ensure each test controls the import outcome explicitly rather than
    # inheriting whatever is already cached in sys.modules.
    monkeypatch.delitem(sys.modules, MODULE_NAME, raising=False)
    yield
    monkeypatch.delitem(sys.modules, MODULE_NAME, raising=False)


class TestTelegramAppCredentials:
    def test_returns_baked_in_credentials_when_module_present(self, monkeypatch):
        fake_module = types.ModuleType(MODULE_NAME)
        fake_module.API_ID = 999
        fake_module.API_HASH = "baked-in-hash"
        monkeypatch.setitem(sys.modules, MODULE_NAME, fake_module)

        assert app_credentials.telegram_app_credentials() == (999, "baked-in-hash")

    def test_baked_in_api_id_is_coerced_to_int(self, monkeypatch):
        fake_module = types.ModuleType(MODULE_NAME)
        fake_module.API_ID = "999"  # some generators might emit a string
        fake_module.API_HASH = "baked-in-hash"
        monkeypatch.setitem(sys.modules, MODULE_NAME, fake_module)

        result = app_credentials.telegram_app_credentials()

        assert result == (999, "baked-in-hash")
        assert isinstance(result[0], int)

    def test_falls_back_to_env_vars_when_module_absent(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)  # force ImportError
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_ID", "123")
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_HASH", "envhash")

        assert app_credentials.telegram_app_credentials() == (123, "envhash")

    def test_none_when_module_absent_and_no_env_vars(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_ID", raising=False)
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_HASH", raising=False)

        assert app_credentials.telegram_app_credentials() is None

    def test_none_when_only_api_id_env_var_set(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_ID", "123")
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_HASH", raising=False)

        assert app_credentials.telegram_app_credentials() is None

    def test_none_when_only_api_hash_env_var_set(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_ID", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_HASH", "envhash")

        assert app_credentials.telegram_app_credentials() is None

    def test_baked_in_module_takes_priority_over_env_vars(self, monkeypatch):
        fake_module = types.ModuleType(MODULE_NAME)
        fake_module.API_ID = 999
        fake_module.API_HASH = "baked-in-hash"
        monkeypatch.setitem(sys.modules, MODULE_NAME, fake_module)
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_ID", "123")
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_HASH", "envhash")

        assert app_credentials.telegram_app_credentials() == (999, "baked-in-hash")


BUILTIN_MODULE_NAME = "privacyfence._builtin_apps"


class TestBuiltinApp:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delitem(sys.modules, BUILTIN_MODULE_NAME, raising=False)
        monkeypatch.delenv("PRIVACYFENCE_BUILTIN_APPS", raising=False)
        yield
        monkeypatch.delitem(sys.modules, BUILTIN_MODULE_NAME, raising=False)

    def _module(self, monkeypatch, apps):
        fake_module = types.ModuleType(BUILTIN_MODULE_NAME)
        fake_module.APPS = apps
        monkeypatch.setitem(sys.modules, BUILTIN_MODULE_NAME, fake_module)

    def test_key_order(self):
        assert app_credentials.BUILTIN_APP_KEYS == (
            "google_web",
            "slack",
            "salesforce",
            "atlassian_relay",
            "miro",
            "google_signin",
        )

    def test_returns_generated_modules_entry(self, monkeypatch):
        self._module(monkeypatch, {"slack": {"client_id": "i", "client_secret": "s"}})

        assert app_credentials.builtin_app("slack") == {"client_id": "i", "client_secret": "s"}
        assert app_credentials.builtin_app("miro") is None

    def test_returns_a_copy(self, monkeypatch):
        apps = {"slack": {"client_id": "i", "client_secret": "s"}}
        self._module(monkeypatch, apps)

        app_credentials.builtin_app("slack")["client_id"] = "changed"

        assert apps["slack"]["client_id"] == "i"

    def test_module_takes_priority_over_the_json_file(self, monkeypatch, tmp_path):
        self._module(monkeypatch, {"slack": {"client_id": "baked", "client_secret": "s"}})
        path = tmp_path / "apps.json"
        path.write_text('{"slack": {"client_id": "file", "client_secret": "s"}}', encoding="utf-8")
        monkeypatch.setenv("PRIVACYFENCE_BUILTIN_APPS", str(path))

        assert app_credentials.builtin_app("slack")["client_id"] == "baked"

    def test_falls_back_to_the_json_file(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, BUILTIN_MODULE_NAME, None)  # force ImportError
        path = tmp_path / "apps.json"
        path.write_text('{"miro": {"client_id": "i", "client_secret": "s"}}', encoding="utf-8")
        monkeypatch.setenv("PRIVACYFENCE_BUILTIN_APPS", str(path))

        assert app_credentials.builtin_app("miro") == {"client_id": "i", "client_secret": "s"}
        assert app_credentials.builtin_app("slack") is None

    def test_none_without_module_or_env_var(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, BUILTIN_MODULE_NAME, None)

        with caplog.at_level("WARNING"):
            assert app_credentials.builtin_app("slack") is None
        assert caplog.records == []

    @pytest.mark.parametrize("content", [None, "not json {", "[1, 2]", '"text"'])
    def test_bad_file_returns_none_and_warns_once_without_its_content(self, monkeypatch, tmp_path, caplog, content):
        monkeypatch.setitem(sys.modules, BUILTIN_MODULE_NAME, None)
        path = tmp_path / "apps.json"
        if content is not None:  # None: the file does not exist
            path.write_text(content, encoding="utf-8")
        monkeypatch.setenv("PRIVACYFENCE_BUILTIN_APPS", str(path))

        with caplog.at_level("WARNING"):
            assert app_credentials.builtin_app("slack") is None

        assert len(caplog.records) == 1
        assert "PRIVACYFENCE_BUILTIN_APPS" in caplog.text
        assert "not json" not in caplog.text and str(tmp_path) not in caplog.text

    def test_non_object_entry_returns_none(self, monkeypatch):
        self._module(monkeypatch, {"slack": "oops"})

        assert app_credentials.builtin_app("slack") is None
