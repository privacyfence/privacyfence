"""Unit tests for privacyfence.resource_names.

Name resolution is purely cosmetic (the menu bar's display layer) -- these
tests focus on the caching/fallback contract: resolution never blocks or
fails loudly, a fresh resolve is cached for reuse, and a failed live lookup
falls back to the last-known name rather than losing it.
"""
from __future__ import annotations

from privacyfence.policy import resource_registry as rg
from privacyfence import resource_names as rn


def _folder_type() -> rg.GrantResourceType:
    rt = rg.resource_type("drive", "folders")
    assert rt is not None
    return rt


class _FakeClient:
    def __init__(self, names: dict[str, str]):
        self._names = names
        self.calls = 0

    def get_file_metadata(self, resource_id: str):
        self.calls += 1
        from types import SimpleNamespace
        return SimpleNamespace(name=self._names.get(resource_id, ""))


class _FailingClient:
    def get_file_metadata(self, resource_id: str):
        raise RuntimeError("simulated API failure")


class TestResolve:
    def test_resolves_and_caches_a_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        client = _FakeClient({"F1": "Q3 Reports"})

        name = resolver.resolve(_folder_type(), "F1", client)

        assert name == "My Drive: Q3 Reports"
        assert client.calls == 1

    def test_second_resolve_within_ttl_does_not_call_the_client_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        client = _FakeClient({"F1": "Q3 Reports"})

        resolver.resolve(_folder_type(), "F1", client)
        resolver.resolve(_folder_type(), "F1", client)

        assert client.calls == 1

    def test_no_client_and_no_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.resolve(_folder_type(), "UNKNOWN", None) is None

    def test_no_client_falls_back_to_previously_cached_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        # Later call with no live connection -- should still know the name.
        assert resolver.resolve(_folder_type(), "F1", None) == "My Drive: Q3 Reports"

    def test_failing_client_falls_back_to_cached_name_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        name = resolver.resolve(_folder_type(), "F1", _FailingClient())

        assert name == "My Drive: Q3 Reports"

    def test_failing_client_with_no_prior_cache_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.resolve(_folder_type(), "F1", _FailingClient()) is None

    def test_empty_name_from_resolver_is_treated_as_unresolved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.resolve(_folder_type(), "MISSING", _FakeClient({})) is None

    def test_different_resource_ids_are_cached_independently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        client = _FakeClient({"F1": "Alpha", "F2": "Beta"})

        assert resolver.resolve(_folder_type(), "F1", client) == "My Drive: Alpha"
        assert resolver.resolve(_folder_type(), "F2", client) == "My Drive: Beta"


class TestCachedName:
    def test_cached_name_before_any_resolve_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()

        assert resolver.cached_name(_folder_type(), "F1") is None

    def test_cached_name_reflects_a_prior_successful_resolve(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        assert resolver.cached_name(_folder_type(), "F1") == "My Drive: Q3 Reports"


class TestDiskPersistence:
    def test_name_survives_across_resolver_instances_via_disk_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(rn, "_cache_file", lambda: cache_file)

        first = rn.ResourceNameResolver()
        first.resolve(_folder_type(), "F1", _FakeClient({"F1": "Q3 Reports"}))

        # Simulate a daemon restart: brand new instance, no live client yet.
        second = rn.ResourceNameResolver()
        assert second.cached_name(_folder_type(), "F1") == "My Drive: Q3 Reports"

    def test_corrupt_disk_cache_is_ignored_not_raised(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")
        monkeypatch.setattr(rn, "_cache_file", lambda: cache_file)

        resolver = rn.ResourceNameResolver()  # must not raise

        assert resolver.cached_name(_folder_type(), "F1") is None

    def test_missing_disk_cache_file_is_treated_as_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "does-not-exist.json")

        resolver = rn.ResourceNameResolver()

        assert resolver.cached_name(_folder_type(), "F1") is None


class TestGetResolverSingleton:
    def test_returns_the_same_instance_across_calls(self):
        # Reset via the per-principal registry rather than a bare
        # module global -- see tests/conftest.py's own autouse reset, which
        # already does this between tests; this test just makes the
        # "same instance for the same (here: default/local) principal"
        # guarantee explicit.
        rn._REGISTRY.reset()
        first = rn.get_resolver()
        second = rn.get_resolver()
        assert first is second


class _DriveClient:
    """A Drive client with one Shared Drive, its root and a second folder inside it."""

    def get_file_metadata(self, resource_id: str):
        from types import SimpleNamespace
        files = {
            "F1": SimpleNamespace(name="Reports", drive_id=""),
            "D1": SimpleNamespace(name="Finance", drive_id="D1"),
            "F2": SimpleNamespace(name="Reports", drive_id="D1"),
            "F3": SimpleNamespace(name="Orphan", drive_id="GONE"),
        }
        return files[resource_id]

    def get_shared_drive_name(self, drive_id: str) -> str:
        if drive_id == "D1":
            return "Finance"
        raise RuntimeError("no such drive")


class TestDriveOrigin:
    def test_names_say_which_drive_they_live_in(self):
        assert rg._resolve_drive_file(_DriveClient(), "F1") == "My Drive: Reports"
        assert rg._resolve_drive_file(_DriveClient(), "F2") == "Finance: Reports"

    def test_a_shared_drives_root_is_just_the_drive_name(self):
        assert rg._resolve_drive_file(_DriveClient(), "D1") == "Finance"

    def test_an_unreadable_shared_drive_name_still_resolves(self):
        assert rg._resolve_drive_file(_DriveClient(), "F3") == "Shared Drive: Orphan"


class TestInvalid:
    def test_a_failed_live_lookup_marks_the_id_invalid_but_keeps_the_last_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        rt = _folder_type()
        resolver.resolve(rt, "F1", _FakeClient({"F1": "Q3"}))
        assert not resolver.is_invalid(rt, "F1")

        monkeypatch.setattr(rn, "CACHE_TTL_SECONDS", 0)
        assert resolver.resolve(rt, "F1", _FakeClient({})) == "My Drive: Q3"
        monkeypatch.setattr(rn, "CACHE_TTL_SECONDS", 900)
        assert resolver.is_invalid(rt, "F1")
        assert resolver.cached_name(rt, "F1") == "My Drive: Q3"

    def test_no_client_is_not_invalid(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        resolver.resolve(_folder_type(), "F1", None)
        assert not resolver.is_invalid(_folder_type(), "F1")
        assert resolver.needs_resolve(_folder_type(), "F1")

    def test_a_later_success_clears_invalid_and_a_fresh_outcome_stops_lookups(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        rt = _folder_type()
        client = _FakeClient({})
        resolver.resolve(rt, "F1", client)
        assert resolver.is_invalid(rt, "F1") and not resolver.needs_resolve(rt, "F1")

        monkeypatch.setattr(rn, "CACHE_TTL_SECONDS", 0)
        resolver.resolve(rt, "F1", _FakeClient({"F1": "Back"}))
        monkeypatch.setattr(rn, "CACHE_TTL_SECONDS", 900)
        assert not resolver.is_invalid(rt, "F1")
        assert resolver.cached_name(rt, "F1") == "My Drive: Back"


class TestInvalidSkipsTheClient:
    def test_a_fresh_not_found_answers_from_the_cache_without_asking_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rn, "_cache_file", lambda: tmp_path / "cache.json")
        resolver = rn.ResourceNameResolver()
        rt = _folder_type()
        resolver.resolve(rt, "F1", _FakeClient({"F1": "Q3"}))
        monkeypatch.setattr(rn, "CACHE_TTL_SECONDS", 0)
        resolver.resolve(rt, "F1", _FakeClient({}))
        monkeypatch.setattr(rn, "CACHE_TTL_SECONDS", 900)
        client = _FakeClient({"F1": "Back"})

        assert resolver.resolve(rt, "F1", client) == "My Drive: Q3"
        assert client.calls == 0
