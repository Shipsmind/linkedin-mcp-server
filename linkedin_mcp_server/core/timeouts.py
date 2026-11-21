"""How long LinkedIn page work may wait, from the browser config (Shipsmind fork).

Waits are on browser events (a selector, a predicate, a URL, a response),
never on fixed times, so they finish as soon as the page is ready on a fast
network and keep waiting on a slow one. These are the deadlines for those
waits:

- :func:`content_ms`: a page's content after navigating (profile top card,
  inbox list, a sent message's confirmation). Default 30 s.
- :func:`control_ms`: an in-page control (a dialog, a button, a field) after
  an action. Default 10 s. Also Playwright's default for any other wait.
- :func:`navigation_ms`: a navigation. Default 30 s.

Human pacing comes after the page is ready, never instead of waiting for it.
"""

from __future__ import annotations

_DEFAULT_CONTENT_MS = 30_000
_DEFAULT_CONTROL_MS = 10_000
_DEFAULT_NAVIGATION_MS = 30_000

# Test-only: a suite of fake pages sets short deadlines so a case that waits
# for something that never comes fails fast, and the slow-network mode sets
# the production ones. Never set in production code.
_OVERRIDE: dict[str, int] = {}


def set_timeouts_for_testing(
    *,
    content: int | None = None,
    control: int | None = None,
    navigation: int | None = None,
) -> None:
    _OVERRIDE.clear()
    for key, value in (
        ("content", content),
        ("control", control),
        ("navigation", navigation),
    ):
        if value is not None:
            _OVERRIDE[key] = int(value)


def _browser_config():
    try:
        from linkedin_mcp_server.config import get_config

        return get_config().browser
    except Exception:  # noqa: BLE001 - tests and tools without a config
        return None


def content_ms() -> int:
    if "content" in _OVERRIDE:
        return _OVERRIDE["content"]
    config = _browser_config()
    return int(
        getattr(config, "content_timeout", _DEFAULT_CONTENT_MS) or _DEFAULT_CONTENT_MS
    )


def control_ms() -> int:
    if "control" in _OVERRIDE:
        return _OVERRIDE["control"]
    config = _browser_config()
    return int(
        getattr(config, "default_timeout", _DEFAULT_CONTROL_MS) or _DEFAULT_CONTROL_MS
    )


def navigation_ms() -> int:
    if "navigation" in _OVERRIDE:
        return _OVERRIDE["navigation"]
    config = _browser_config()
    return int(
        getattr(config, "navigation_timeout", _DEFAULT_NAVIGATION_MS)
        or _DEFAULT_NAVIGATION_MS
    )
