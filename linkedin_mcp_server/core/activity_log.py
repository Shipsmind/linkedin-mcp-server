"""Activity log of what the human-paced LinkedIn input did (Shipsmind fork).

One event per step: pauses (planned and actual), mouse moves, clicks, wheel
bursts, typing runs, navigations, page loads, every security-watch signal
(including near-misses the watch deliberately ignored) and tripwire trips.
The operator's tooling installs a sink with :func:`set_activity_sink`; pm-platform
writes them to ``~/.config/shipsmind/connectors/linkedin-activity-YYYY-MM-DD.jsonl``
so a check can be traced back to the pacing that preceded it.

Typed text is NEVER logged: a ``type`` event carries counts and timing only.
:func:`emit` never raises; a failing sink is ignored, because the log must
never be the reason an action stops (or does not stop) half-way.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

ActivitySink = Callable[[dict[str, Any]], None]

_sink: ActivitySink | None = None


def set_activity_sink(sink: ActivitySink | None) -> None:
    """Install the function that receives each event (one dict); None turns it off."""
    global _sink
    _sink = sink


def activity_sink() -> ActivitySink | None:
    return _sink


def emit(step: str, **fields: Any) -> None:
    """Send one event to the sink, if any. Never raises."""
    sink = _sink
    if sink is None:
        return
    try:
        event: dict[str, Any] = {"t": time.time(), "step": step}
        event.update(fields)
        sink(event)
    except Exception:  # noqa: BLE001 - the log never breaks or blocks input
        logger.debug("activity sink failed for %s", step, exc_info=True)
