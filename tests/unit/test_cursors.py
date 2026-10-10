"""The cursor envelope: round trip, and refusal of every cursor that is not for this call."""
from __future__ import annotations

import base64
import json

import pytest

from privacyfence import cursors

pytestmark = pytest.mark.unit

BOUND = {"jql": "project = X", "page_size": 50}
STATE = {"t": "tok", "k": 3}


def _raw(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


class TestRoundTrip:
    def test_decode_returns_the_state(self):
        cursor = cursors.encode("jira.search", BOUND, STATE)
        assert cursors.decode(cursor, "jira.search", BOUND) == STATE

    def test_cursor_is_unpadded_urlsafe(self):
        cursor = cursors.encode("jira.search", BOUND, STATE)
        assert "=" not in cursor and "+" not in cursor and "/" not in cursor

    def test_digest_ignores_key_order(self):
        a = cursors.params_digest("op", {"a": 1, "b": 2})
        assert a == cursors.params_digest("op", {"b": 2, "a": 1})

    def test_digest_depends_on_operation(self):
        assert cursors.params_digest("a", BOUND) != cursors.params_digest("b", BOUND)


class TestRefusal:
    def test_wrong_operation(self):
        cursor = cursors.encode("jira.search", BOUND, STATE)
        with pytest.raises(cursors.CursorError, match="different call"):
            cursors.decode(cursor, "calendar.list_events", BOUND)

    def test_wrong_params(self):
        cursor = cursors.encode("jira.search", BOUND, STATE)
        with pytest.raises(cursors.CursorError, match="different call"):
            cursors.decode(cursor, "jira.search", {**BOUND, "page_size": 10})

    @pytest.mark.parametrize("cursor", ["!!!not base64!!!", "a", "", "é"])
    def test_bad_base64(self, cursor):
        with pytest.raises(cursors.CursorError, match="cursor is not valid"):
            cursors.decode(cursor, "jira.search", BOUND)

    def test_not_json(self):
        cursor = base64.urlsafe_b64encode(b"not json").decode()
        with pytest.raises(cursors.CursorError, match="cursor is not valid"):
            cursors.decode(cursor, "jira.search", BOUND)

    @pytest.mark.parametrize(
        "obj",
        [
            [],
            {},
            {"v": 2, "op": "jira.search", "d": "x", "s": {}},
            {"v": 1, "op": 1, "d": "x", "s": {}},
            {"v": 1, "op": "jira.search", "d": 1, "s": {}},
            {"v": 1, "op": "jira.search", "d": "x", "s": []},
            {"v": 1, "op": "jira.search", "d": "x"},
        ],
    )
    def test_bad_shape(self, obj):
        with pytest.raises(cursors.CursorError, match="cursor is not valid"):
            cursors.decode(_raw(obj), "jira.search", BOUND)

    def test_overlong(self):
        cursor = cursors.encode("jira.search", BOUND, {"t": "x" * cursors.CURSOR_MAX_CHARS})
        assert len(cursor) > cursors.CURSOR_MAX_CHARS
        with pytest.raises(cursors.CursorError, match="cursor is not valid"):
            cursors.decode(cursor, "jira.search", BOUND)

    def test_non_string(self):
        with pytest.raises(cursors.CursorError, match="cursor is not valid"):
            cursors.decode(None, "jira.search", BOUND)  # type: ignore[arg-type]


class TestHandEditedState:
    """The state is not signed: an edited one still decodes, but only for the same parameters."""

    def _edited(self) -> str:
        envelope = json.loads(
            base64.urlsafe_b64decode(
                (c_ := cursors.encode("jira.search", BOUND, STATE)) + "=" * (-len(c_) % 4)
            )
        )
        envelope["s"] = {"t": "other", "k": 99}
        return _raw(envelope)

    def test_edited_state_decodes(self):
        assert cursors.decode(self._edited(), "jira.search", BOUND) == {"t": "other", "k": 99}

    def test_edited_state_with_other_params_is_refused(self):
        with pytest.raises(cursors.CursorError, match="different call"):
            cursors.decode(self._edited(), "jira.search", {**BOUND, "jql": "other"})
