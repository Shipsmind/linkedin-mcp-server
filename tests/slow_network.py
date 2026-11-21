"""Slow-network test mode for the DOM suites (Shipsmind fork).

``LINKEDIN_TEST_SLOW_NETWORK=1`` runs the fake LinkedIn pages the way a slow
user network and a slow machine see them, so waits that only worked because
the local pages answer instantly fail here:

- Chrome DevTools ``Network.emulateNetworkConditions``: 400 ms latency and
  about 400 kbit/s each way;
- every routed (fulfilled) response is held for the same latency first,
  because fulfilled responses do not go through the emulated network;
- ``Emulation.setCPUThrottlingRate`` 4x, so the pages' own scripts (which
  stand in for LinkedIn's rendering) run slowly too.

In this mode the production deadlines apply (content 30 s, controls 10 s);
otherwise the suite uses short ones so a case that waits for something that
never comes fails fast.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

SLOW_NETWORK = os.environ.get("LINKEDIN_TEST_SLOW_NETWORK", "").strip() in {
    "1",
    "true",
    "on",
}

LATENCY_MS = 400
THROUGHPUT_BYTES_PER_S = 50_000  # about 400 kbit/s
CPU_SLOWDOWN = 4


async def apply_slow_network(page: Any) -> None:
    """Throttle *page* when the slow-network mode is on. Safe to call always."""
    if not SLOW_NETWORK:
        return
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Network.enable")
    await cdp.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": LATENCY_MS,
            "downloadThroughput": THROUGHPUT_BYTES_PER_S,
            "uploadThroughput": THROUGHPUT_BYTES_PER_S,
        },
    )
    await cdp.send("Emulation.setCPUThrottlingRate", {"rate": CPU_SLOWDOWN})
    original_route = page.route

    async def slow_route(url: Any, handler: Any, *args: Any, **kwargs: Any) -> Any:
        async def delayed(route: Any, *rest: Any) -> Any:
            await asyncio.sleep(LATENCY_MS / 1000)
            result = handler(route, *rest)
            if asyncio.iscoroutine(result):
                return await result
            return result

        return await original_route(url, delayed, *args, **kwargs)

    page.route = slow_route
