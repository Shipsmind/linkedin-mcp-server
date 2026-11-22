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


# --- wait_page_ready on a real page -------------------------------------------

from patchright.async_api import async_playwright  # noqa: E402

from linkedin_mcp_server.linkedin.page_ready import READY_JS  # noqa: E402
from linkedin_mcp_server.linkedin.session import PageSession  # noqa: E402
from slow_network import apply_slow_network  # noqa: E402

LATE_TOP_CARD = """<!DOCTYPE html><html><body><main id="m"></main><script>
setTimeout(() => {
  document.getElementById('m').innerHTML =
    '<section><h1>Ada Lovelace</h1><button>Message</button></section>';
}, 700);
</script></body></html>"""


@pytest.fixture
async def real_page():
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        await apply_slow_network(page)
        try:
            yield page
        finally:
            await browser.close()


def test_every_ready_kind_is_a_marked_function():
    for kind, script in READY_JS.items():
        assert script.startswith("() => {") and f"/* page-ready:{kind} */" in script


async def test_the_profile_wait_returns_once_the_top_card_renders(real_page):
    await real_page.set_content(LATE_TOP_CARD)
    started = time.monotonic()
    assert await PageSession(real_page).wait_page_ready("profile", timeout=10_000)
    assert time.monotonic() - started >= 0.5  # waited for the late render
    assert await real_page.evaluate("document.querySelector('main h1').innerText")


async def test_an_empty_page_is_not_ready_and_the_wait_ends_at_its_deadline(real_page):
    await real_page.set_content(
        "<!DOCTYPE html><html><body><main></main></body></html>"
    )
    started = time.monotonic()
    assert not await PageSession(real_page).wait_page_ready("main", timeout=600)
    assert time.monotonic() - started < 5


def test_the_job_sidebar_waits_grow_with_the_configured_deadlines():
    from linkedin_mcp_server.linkedin.job_pages import _secs

    set_timeouts_for_testing(content=30_000, control=10_000)
    assert _secs(5.0, 10_000) == 10.0  # never shorter than the configured one
    assert _secs(12.0, 10_000) == 12.0  # never shorter than what it replaced
