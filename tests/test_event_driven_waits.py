"""Waits on browser events with realistic deadlines (Shipsmind fork)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from linkedin_mcp_server.config.schema import BrowserConfig
from linkedin_mcp_server.core import security_watch as sw
from linkedin_mcp_server.core.exceptions import SecurityCheckError
from linkedin_mcp_server.core.timeouts import set_timeouts_for_testing
from linkedin_mcp_server.linkedin import connection
from linkedin_mcp_server.linkedin.connection_actions import ConnectionActions
from linkedin_mcp_server.linkedin.message_sender import MessageSender


def test_the_default_deadlines_fit_a_slow_network():
    config = BrowserConfig()
    assert config.default_timeout == 10_000  # in-page controls (was 5000)
    assert config.content_timeout == 30_000  # page content after navigating
    assert config.navigation_timeout == 30_000


class _Session:
    def __init__(self, states: list[str]):
        self.states = states
        self.reads = 0
        self.polls = 0

    async def delay(self, _seconds: float) -> None:
        self.polls += 1


def _actions(states: list[str]) -> tuple[ConnectionActions, _Session]:
    session = _Session(states)
    actions = ConnectionActions.__new__(ConnectionActions)
    actions._session = session  # type: ignore[attr-defined]

    async def read(_username: str) -> Any:
        state = session.states[min(session.reads, len(session.states) - 1)]
        session.reads += 1
        return state

    actions._read_action_signals = read  # type: ignore[method-assign]
    return actions, session


async def test_the_relationship_wait_ends_when_the_top_card_changes(monkeypatch):
    monkeypatch.setattr(connection, "detect_connection_state", lambda s: s)
    actions, session = _actions(
        ["incoming_request", "incoming_request", "already_connected"]
    )
    assert await actions._wait_for_relationship_state("ada", ("already_connected",))
    assert session.reads == 3 and session.polls == 2


async def test_the_relationship_wait_gives_up_at_the_content_deadline(monkeypatch):
    monkeypatch.setattr(connection, "detect_connection_state", lambda s: s)
    set_timeouts_for_testing(content=200, control=200)
    actions, session = _actions(["connectable"])

    async def real_delay(seconds: float) -> None:
        await anyio.sleep(0.05)

    session.delay = real_delay  # type: ignore[method-assign]
    started = time.monotonic()
    assert not await actions._wait_for_relationship_state("ada", ("pending",))
    assert 0.15 <= time.monotonic() - started < 2.0


async def test_the_relationship_wait_never_swallows_a_security_check(monkeypatch):
    actions, _ = _actions(["connectable"])

    async def tripped(_username: str) -> Any:
        sw.TRIPWIRE.trip(sw.CHECK, "response-999", "https://www.linkedin.com/x")
        sw.raise_if_tripped()

    actions._read_action_signals = tripped  # type: ignore[method-assign]
    with pytest.raises(SecurityCheckError):
        await actions._wait_for_relationship_state("ada", ("pending",))


async def test_the_send_confirmation_waits_for_the_rest_of_the_send_budget():
    page = MagicMock()
    handle = MagicMock()
    handle.json_value = AsyncMock(return_value=None)
    handle.dispose = AsyncMock()
    page.wait_for_function = AsyncMock(return_value=handle)
    session = MagicMock()
    session.page = page
    sender = MessageSender(session, MagicMock())
    sender._message_target_argument = lambda target: {}  # type: ignore[method-assign]

    with anyio.CancelScope(deadline=anyio.current_time() + 20):
        await sender._message_send_confirmed(
            "Hi", target=MagicMock(), owner=MagicMock(), confirmation="t"
        )
    timeout = page.wait_for_function.await_args.kwargs["timeout"]
    assert 18_000 < timeout <= 20_000  # the budget, not the 5 s page default
