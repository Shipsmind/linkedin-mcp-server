"""Slow-network test mode for the DOM suites (Shipsmind fork).

``LINKEDIN_TEST_SLOW_NETWORK`` runs the fake LinkedIn pages the way a slow
home network and a slow machine see them, so waits that only worked because
the local pages answer instantly fail here. Two profiles, from Chrome
DevTools' own presets:

- ``1`` / ``fast3g`` (the suite's slow mode): "Fast 3G", 562.5 ms latency,
  about 1.6 Mbit/s down and 750 kbit/s up. This is the slow end of a real
  home connection (a congested or rural line, a phone hotspot), which is
  what the operator's network can actually be.
- ``slow3g`` (a manual stress run): "Slow 3G", 2 s latency, about 400 kbit/s
  each way. Slower than any home connection the connector is meant for.

Each profile applies, through Chrome DevTools:

- ``Network.emulateNetworkConditions`` with the latency and throughput;
- the same latency on every routed (fulfilled) response, held in Python,
  because fulfilled responses do not go through the emulated network;
- ``Emulation.setCPUThrottlingRate`` 4x, so the pages' own scripts (which
  stand in for LinkedIn's rendering) run as on a slow machine.

In either mode the production deadlines apply (content 30 s, controls 10 s);
otherwise the suite uses short ones so a case that waits for something that
never comes fails fast.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any

# Chrome DevTools presets. Throughput in bytes per second, as the CDP takes it
# (DevTools' own figures, which include its 0.9 / 0.8 derating).
PROFILES: dict[str, dict[str, float]] = {
    "fast3g": {
        "latency": 562.5,
        "download": 1.6 * 1024 * 1024 / 8 * 0.9,
        "upload": 750 * 1024 / 8 * 0.9,
        "cpu": 4,
    },
    "slow3g": {
        "latency": 2000.0,
        "download": 500 * 1024 / 8 * 0.8,
        "upload": 500 * 1024 / 8 * 0.8,
        "cpu": 4,
    },
}

_RAW = os.environ.get("LINKEDIN_TEST_SLOW_NETWORK", "").strip().lower()
if _RAW in {"1", "true", "on", "fast3g"}:
    PROFILE_NAME: str | None = "fast3g"
elif _RAW == "slow3g":
    PROFILE_NAME = "slow3g"
else:
    PROFILE_NAME = None
SLOW_NETWORK = PROFILE_NAME is not None
PROFILE = PROFILES.get(PROFILE_NAME or "", {})


async def apply_slow_network(
    page: Any, profile: dict[str, float] | None = None
) -> None:
    """Throttle *page* when the slow-network mode is on. Safe to call always."""
    profile = profile or PROFILE
    if not profile:
        return
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Network.enable")
    await cdp.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": profile["latency"],
            "downloadThroughput": profile["download"],
            "uploadThroughput": profile["upload"],
        },
    )
    await cdp.send("Emulation.setCPUThrottlingRate", {"rate": profile["cpu"]})
    original_route = page.route
    latency_s = profile["latency"] / 1000

    async def slow_route(url: Any, handler: Any, *args: Any, **kwargs: Any) -> Any:
        # Pass the handler exactly the arguments it takes: Playwright calls a
        # handler with (route) or (route, request) by its own arity, and a
        # one-argument lambda given both raises and leaves the route hanging.
        try:
            wants_request = len(inspect.signature(handler).parameters) >= 2
        except (TypeError, ValueError):
            wants_request = False

        async def delayed(route: Any, request: Any) -> Any:
            await asyncio.sleep(latency_s)
            result = handler(route, request) if wants_request else handler(route)
            if asyncio.iscoroutine(result):
                return await result
            return result

        return await original_route(url, delayed, *args, **kwargs)

    page.route = slow_route
