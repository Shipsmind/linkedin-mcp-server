"""Ordinary browsing between outreach steps (Shipsmind fork).

LinkedIn's own abuse model scores the ORDER of a member's request types: a
run of profile views with nothing in between is the textbook scraping shape.
These workflows let the operator's tooling mix in what a person does between
those views, and reach a profile the way a person does:

- :meth:`Browsing.browse_feed`: go to the home feed (the header link when on
  LinkedIn) and scroll it for a few screens with reading pauses.
- :meth:`Browsing.glance_page`: look at My Network or Messaging and scroll a
  little. Nothing is ever clicked there: opening a thread marks it read and
  an invitation button is a write. Notifications are excluded on purpose,
  because visiting them marks them seen.
- :meth:`Browsing.open_profile_via_search`: type the name into the global
  search box and open the matching person from the typeahead or the people
  results. Name searches from the top box are exempt from LinkedIn's
  commercial use limit (help article 52950).

Every input goes through ``core.human_input`` (random pacing, real mouse and
keys, security tripwire on every step), and each step is logged through
``core.activity_log``. Nothing here reads or returns page content.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.core import human_input
from linkedin_mcp_server.core.activity_log import emit
from linkedin_mcp_server.core.security_watch import is_linkedin_host, raise_if_tripped
from linkedin_mcp_server.core.timeouts import content_ms, control_ms
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

LINKEDIN = "https://www.linkedin.com"

# Header navigation links, by the address they lead to (no class names or
# English labels, per the selector policy).
_FEED_NAV = 'header a[href*="/feed/"]'
_GLANCE_PATHS = {"my_network": "/mynetwork/", "messaging": "/messaging/"}
_GLANCE_NAV = {
    "my_network": 'header a[href*="/mynetwork/"]',
    "messaging": 'header a[href*="/messaging/"]',
}

# The global search box is the header's combobox input.
_SEARCH_BOX = 'header input[role="combobox"]'
_TYPEAHEAD = '[role="listbox"]'
_TYPEAHEAD_PROFILE_LINKS = '[role="listbox"] a[href*="/in/"]'
_RESULT_PROFILE_LINKS = 'main a[href*="/in/"]'

_USERNAME = re.compile(r"^[A-Za-z0-9\-_%.]{2,100}$")

# Index of the first link whose path is exactly /in/<username>/ (case-blind),
# among the links the given selector finds. Read-only.
_FIRST_PROFILE_LINK_JS = r"""({ selector, username }) => {
    const wanted = '/in/' + username.toLowerCase().replace(/\/+$/, '') + '/';
    const links = Array.from(document.querySelectorAll(selector));
    for (let i = 0; i < links.length; i++) {
        let path;
        try {
            path = new URL(links[i].getAttribute('href'), location.href).pathname;
        } catch {
            continue;
        }
        path = decodeURIComponent(path).toLowerCase();
        if (!path.endsWith('/')) path += '/';
        if (path === wanted) return i;
    }
    return -1;
}"""

_VISIBLE_TEXT_LENGTH_JS = r"""() => {
    const main = document.querySelector('main') || document.body;
    return (main && main.innerText ? main.innerText.length : 0);
}"""

# How long a click-driven route change may take before it counts as not taken.
_ROUTE_POLLS = 40
_ROUTE_POLL_SECONDS = 0.25


def _path(url: str) -> str:
    path = urlparse(url or "").path or "/"
    return path if path.endswith("/") else path + "/"


class Browsing:
    """Feed, glance and search-to-profile, at a person's pace."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    # --- shared steps ---------------------------------------------------------

    @property
    def _page(self) -> Any:
        return self._session.page

    def _on_linkedin(self) -> bool:
        url = str(getattr(self._page, "url", "") or "")
        return is_linkedin_host((urlparse(url).hostname or "").lower())

    async def _visible(self, selector: str) -> Any | None:
        """The first visible match of *selector*, or None. Read-only."""
        locator = self._page.locator(selector).first
        try:
            if await self._page.locator(selector).count() == 0:
                return None
            if not await locator.is_visible():
                return None
        except Exception:  # noqa: BLE001 - a page mid-navigation
            return None
        return locator

    async def _wait_for_path(self, path: str) -> bool:
        """Poll the address until its path is *path* (a click's SPA route)."""
        for _ in range(_ROUTE_POLLS):
            raise_if_tripped()
            if _path(str(self._page.url)).lower() == path.lower():
                return True
            await self._session.delay(_ROUTE_POLL_SECONDS)
        return _path(str(self._page.url)).lower() == path.lower()

    async def _go(self, path: str, nav_selector: str | None) -> None:
        """Reach *path* via the header link when on LinkedIn, else navigate."""
        if _path(str(self._page.url)) == path and self._on_linkedin():
            return
        if nav_selector and self._on_linkedin():
            link = await self._visible(nav_selector)
            if link is not None:
                emit("navigate", url=LINKEDIN + path, via="header-link")
                await human_input.move_and_click(self._page, link)
                if await self._wait_for_path(path):
                    return
                logger.debug("Header link did not route to %s; navigating", path)
        await self._session.page_load_pause()
        await self._navigator._navigate_to_page(LINKEDIN + path)

    async def _read_screen(self) -> None:
        try:
            length = int(await self._session.run_on_linkedin(_VISIBLE_TEXT_LENGTH_JS))
        except Exception:  # noqa: BLE001 - reading is best effort
            length = 0
        await self._session.read_pause(length)

    async def _scroll_screens(self, screens: int) -> None:
        pacer = human_input.pacer()
        rng = pacer.rng
        size = getattr(self._page, "viewport_size", None) or {}
        width = float(size.get("width") or 1280)
        height = float(size.get("height") or 800)
        await human_input.move_mouse_to(
            self._page,
            rng.uniform(width * 0.35, width * 0.65),
            rng.uniform(height * 0.35, height * 0.7),
        )
        for _ in range(screens):
            await human_input.wheel_burst(self._page, rng.randint(2, 6))
            await self._read_screen()

    # --- 1. the feed ---------------------------------------------------------

    async def browse_feed(self, max_screens: int | None = None) -> dict[str, Any]:
        """Scroll the home feed for a few screens. Clicks nothing on it."""
        await self._go("/feed/", _FEED_NAV)
        # The feed's posts rendered (an event wait), then a person's settle.
        await self._session.wait_page_ready("feed")
        await human_input.settle_after_load()
        rng = human_input.pacer().rng
        screens = rng.randint(1, 4)
        if max_screens is not None:
            screens = max(0, min(screens, int(max_screens)))
        await self._read_screen()
        await self._scroll_screens(screens)
        return {"url": str(self._page.url), "screens": screens}

    # --- 2. a glance at My Network / Messaging -------------------------------

    async def glance_page(self, kind: str) -> dict[str, Any]:
        """Look at My Network or Messaging; never click anything there."""
        if kind not in _GLANCE_PATHS:
            raise ValueError(
                f"glance_page kind must be one of {sorted(_GLANCE_PATHS)}, got {kind!r}"
            )
        path = _GLANCE_PATHS[kind]
        await self._go(path, _GLANCE_NAV[kind])
        await self._session.wait_page_ready("inbox" if kind == "messaging" else "main")
        await human_input.settle_after_load()
        await self._read_screen()
        screens = human_input.pacer().rng.randint(0, 3)
        if screens:
            await self._scroll_screens(screens)
        return {"url": str(self._page.url), "kind": kind, "screens": screens}

    # --- 3. a profile reached through the search box ---------------------------

    async def open_profile_via_search(self, name: str, username: str) -> bool:
        """Open /in/<username>/ by typing *name* in the global search box.

        Answers False, without navigating anywhere else, when the box is not
        on the current page or neither the typeahead nor the people results
        hold that profile.
        """
        username = (username or "").strip().strip("/")
        if not _USERNAME.match(username) or not (name or "").strip():
            raise ValueError(
                "open_profile_via_search needs a name and a profile username"
            )
        wanted = f"/in/{username.lower()}/"
        if not self._on_linkedin():
            return False
        box = await self._visible(_SEARCH_BOX)
        if box is None:
            emit("search_box_missing", url=str(self._page.url))
            return False

        await human_input.clear_and_type(self._page, box, name.strip())
        try:
            await self._page.wait_for_selector(
                _TYPEAHEAD, state="visible", timeout=control_ms()
            )
        except Exception:  # noqa: BLE001 - no typeahead: the results page decides
            pass
        await human_input.action_pause()

        if await self._click_profile_link(_TYPEAHEAD_PROFILE_LINKS, username):
            return await self._landed(wanted)

        await human_input.press_key(self._page, "Enter")
        try:
            await self._page.wait_for_selector(
                _RESULT_PROFILE_LINKS, state="attached", timeout=content_ms()
            )
        except Exception:  # noqa: BLE001 - no results at all
            emit("search_no_match", via="results")
            return False
        await human_input.settle_after_load()
        await self._read_screen()
        if await self._click_profile_link(_RESULT_PROFILE_LINKS, username):
            return await self._landed(wanted)
        emit("search_no_match", via="results")
        return False

    async def _click_profile_link(self, selector: str, username: str) -> bool:
        try:
            index = await self._session.run_on_linkedin(
                _FIRST_PROFILE_LINK_JS, {"selector": selector, "username": username}
            )
        except Exception:  # noqa: BLE001 - the list re-rendered under the read
            return False
        if not isinstance(index, int) or index < 0:
            return False
        link = self._page.locator(selector).nth(index)
        emit("navigate", url=f"{LINKEDIN}/in/{username}/", via="search-result")
        await human_input.move_and_click(self._page, link)
        return True

    async def _landed(self, wanted: str) -> bool:
        if await self._wait_for_path(wanted):
            # The profile's top card rendered before anything reads it.
            await self._session.wait_page_ready("profile")
            await human_input.settle_after_load()
            return True
        # Case or encoding may differ between the link and the address.
        return _path(str(self._page.url)).lower() == wanted
