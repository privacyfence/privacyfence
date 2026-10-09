"""Unit tests for privacyfence.plugins.events."""
from __future__ import annotations

import pytest

from privacyfence.plugins.events import EVENT_STATE_CHANGED, EventFanout, transitions
from privacyfence.plugins.rpc import RpcError

pytestmark = pytest.mark.unit


def row(key: str, enabled: bool, authed: bool) -> dict:
    return {"key": key, "enabled": enabled, "authed": authed}


class FakePeer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    async def notify(self, method: str, params: dict) -> None:
        if self.fail:
            raise RpcError("internal_error", "peer closed")
        self.sent.append((method, params))


class TestTransitions:
    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            ((False, False), (True, False), "enabled"),
            ((False, False), (True, True), "enabled"),
            ((True, True), (False, False), "disabled"),
            ((True, False), (False, False), "disabled"),
            ((True, False), (True, True), "signed_in"),
            ((True, True), (True, False), "signed_out"),
            ((True, True), (True, True), None),
            ((True, False), (True, False), None),
            ((False, False), (False, False), None),
            ((False, True), (False, False), None),
        ],
    )
    def test_each_transition(self, previous, current, expected):
        assert transitions(previous, current) == expected


class TestChanges:
    def test_first_call_is_only_a_baseline(self):
        fanout = EventFanout(lambda: [])

        assert fanout.changes([row("gmail", True, True)]) == []

    def test_unchanged_rows_send_nothing(self):
        fanout = EventFanout(lambda: [])
        fanout.changes([row("gmail", True, True), row("drive", False, False)])

        assert fanout.changes([row("gmail", True, True), row("drive", False, False)]) == []

    def test_each_change_is_reported_for_the_local_principal(self):
        fanout = EventFanout(lambda: [])
        fanout.changes([row("gmail", True, False), row("drive", True, True), row("slack", True, True)])

        events = fanout.changes([row("gmail", True, True), row("drive", False, False), row("slack", True, False)])

        assert events == [
            {"connector": "gmail", "state": "signed_in", "principal": "local"},
            {"connector": "drive", "state": "disabled", "principal": "local"},
            {"connector": "slack", "state": "signed_out", "principal": "local"},
        ]

    def test_a_connector_seen_for_the_first_time_is_not_a_change(self):
        fanout = EventFanout(lambda: [])
        fanout.changes([row("gmail", True, True)])

        assert fanout.changes([row("gmail", True, True), row("drive", True, True)]) == []

    def test_the_change_is_only_reported_once(self):
        fanout = EventFanout(lambda: [])
        fanout.changes([row("gmail", False, False)])

        assert len(fanout.changes([row("gmail", True, False)])) == 1
        assert fanout.changes([row("gmail", True, False)]) == []


class TestSend:
    async def test_every_running_plugin_gets_every_event(self):
        first, second = FakePeer(), FakePeer()
        fanout = EventFanout(lambda: [first, second])
        events = [{"connector": "gmail", "state": "enabled", "principal": "local"}]

        await fanout.send(events)

        assert first.sent == second.sent == [(EVENT_STATE_CHANGED, events[0])]

    async def test_a_failed_send_is_swallowed_and_the_others_still_get_it(self):
        broken, healthy = FakePeer(fail=True), FakePeer()
        fanout = EventFanout(lambda: [broken, healthy])

        await fanout.send([{"connector": "gmail", "state": "enabled", "principal": "local"}])

        assert len(healthy.sent) == 1

    async def test_no_running_plugin_is_fine(self):
        await EventFanout(lambda: []).send([{"connector": "gmail", "state": "enabled", "principal": "local"}])
