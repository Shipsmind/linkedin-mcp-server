"""Feed, glance and search-to-profile browsing (Shipsmind fork)."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from linkedin_mcp_server.core import activity_log
from linkedin_mcp_server.core import security_watch as sw
from linkedin_mcp_server.core.exceptions import SecurityCheckError
from linkedin_mcp_server.linkedin import browsing as browsing_module
from linkedin_mcp_server.linkedin.browsing import Browsing

FEED = "https://www.linkedin.com/feed/"


class TimeoutError_(Exception):
    pass


class FakeElement:
    def __init__(
        self,
        box: dict[str, float],
        href: str | None = None,
        on_click: Callable[[], None] | None = None,
        name: str = "",
    ):
        self.box, self.href, self.on_click, self.name = box, href, on_click, name


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str, index: int = 0):
        self.page, self.selector, self.index = page, selector, index

    def _items(self) -> list[FakeElement]:
        return self.page.elements.get(self.selector, [])

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self.page, self.selector, 0)

    def nth(self, i: int) -> "FakeLocator":
        return FakeLocator(self.page, self.selector, i)

    async def count(self) -> int:
        return len(self._items())

    async def is_visible(self) -> bool:
        return bool(self._items())

    async def inner_text(self, timeout: int | None = None) -> str:
        return ""

    async def bounding_box(self) -> dict[str, float] | None:
        items = self._items()
        if self.index >= len(items):
            return None
        self.page.armed = items[self.index]
        return items[self.index].box


class FakeMouse:
    def __init__(self, page: "FakePage"):
        self.page = page

    async def move(self, x: float, y: float) -> None:
        self.page.log.append(("move",))

    async def down(self) -> None:
        self.page.log.append(("down",))

    async def up(self) -> None:
        element = self.page.armed
        self.page.log.append(("click", element.name if element else None))
        if element and element.on_click:
            element.on_click()

    async def wheel(self, dx: float, dy: float) -> None:
        self.page.log.append(("wheel",))


class FakeKeyboard:
    def __init__(self, page: "FakePage"):
        self.page = page

    async def type(self, text: str) -> None:
        self.page.typed.append(text)
        if self.page.on_type:
            self.page.on_type(len(self.page.typed))

    async def press(self, key: str) -> None:
        self.page.log.append(("press", key))
        if key == "Enter" and self.page.on_enter:
            self.page.on_enter()

    async def insert_text(self, text: str) -> None:
        self.page.typed.append(text)


class FakePage:
    viewport_size = {"width": 1280, "height": 800}
    frames: list[Any] = []

    def __init__(self, url: str = FEED):
        self.url = url
        self.elements: dict[str, list[FakeElement]] = {}
        self.log: list[tuple] = []
        self.typed: list[str] = []
        self.armed: FakeElement | None = None
        self.on_enter: Callable[[], None] | None = None
        self.on_type: Callable[[int], None] | None = None
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def wait_for_selector(self, selector: str, **_: Any) -> None:
        if not self.elements.get(selector):
            raise TimeoutError_(selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "wanted = '/in/'" in script and isinstance(arg, dict):
            wanted = "/in/" + arg["username"].lower() + "/"
            for i, el in enumerate(self.elements.get(arg["selector"], [])):
                path = (el.href or "").split("?")[0].lower()
                if not path.endswith("/"):
                    path += "/"
                if path.endswith(wanted):
                    return i
            return -1
        return 500  # visible text length


class FakeSession:
    def __init__(self, page: FakePage):
        self.page = page
        self.pauses: list[str] = []

    async def run_on_linkedin(self, script: str, arg: Any = None) -> Any:
        return await self.page.evaluate(script, arg)

    async def delay(self, _seconds: float) -> None:
        return None

    async def read_pause(self, _text_len: int) -> float:
        self.pauses.append("read")
        return 0.0

    async def page_load_pause(self) -> float:
        self.pauses.append("page_load")
        return 0.0

    async def wait_page_ready(self, kind: str = "main", timeout: Any = None) -> bool:
        self.pauses.append(f"ready:{kind}")
        return True


class FakeNavigator:
    def __init__(self, page: FakePage):
        self.page = page
        self.visits: list[str] = []

    async def _navigate_to_page(self, url: str, **_: Any) -> None:
        self.visits.append(url)
        self.page.url = url


def _box(y: float = 20) -> dict[str, float]:
    return {"x": 100, "y": y, "width": 120, "height": 30}


def _browsing(page: FakePage) -> tuple[Browsing, FakeNavigator, FakeSession]:
    session, navigator = FakeSession(page), FakeNavigator(page)
    return Browsing(session, navigator), navigator, session  # type: ignore[arg-type]


def _clicks(page: FakePage) -> list[str | None]:
    return [entry[1] for entry in page.log if entry[0] == "click"]


# --- browse_feed ----------------------------------------------------------------


async def test_browse_feed_clicks_the_header_home_link_when_on_linkedin():
    page = FakePage("https://www.linkedin.com/in/someone/")
    page.elements[browsing_module._FEED_NAV] = [
        FakeElement(_box(), on_click=lambda: setattr(page, "url", FEED), name="home")
    ]
    browsing, navigator, _ = _browsing(page)
    result = await browsing.browse_feed(max_screens=3)
    assert _clicks(page) == ["home"]
    assert navigator.visits == []
    assert result["url"] == FEED and 1 <= result["screens"] <= 3
    assert sum(1 for e in page.log if e[0] == "wheel") >= 2 * result["screens"]


async def test_browse_feed_navigates_when_not_on_linkedin_and_clicks_no_posts():
    page = FakePage("about:blank")
    browsing, navigator, session = _browsing(page)
    result = await browsing.browse_feed()
    assert navigator.visits == [FEED]
    assert _clicks(page) == []  # nothing on the feed is ever clicked
    assert 1 <= result["screens"] <= 4
    assert session.pauses.count("read") == result["screens"] + 1


# --- glance_page ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "path"), [("my_network", "/mynetwork/"), ("messaging", "/messaging/")]
)
async def test_glance_page_uses_the_header_link_and_clicks_nothing_else(kind, path):
    page = FakePage()
    target = "https://www.linkedin.com" + path
    page.elements[browsing_module._GLANCE_NAV[kind]] = [
        FakeElement(_box(), on_click=lambda: setattr(page, "url", target), name="nav")
    ]
    # Things that must never be clicked on these pages.
    page.elements["main li"] = [FakeElement(_box(300), name="conversation")]
    browsing, navigator, _ = _browsing(page)
    result = await browsing.glance_page(kind)
    assert _clicks(page) == ["nav"]
    assert navigator.visits == []
    assert result["kind"] == kind and 0 <= result["screens"] <= 3


async def test_glance_page_navigates_without_a_header_link():
    page = FakePage()
    browsing, navigator, _ = _browsing(page)
    await browsing.glance_page("messaging")
    assert navigator.visits == ["https://www.linkedin.com/messaging/"]
    assert _clicks(page) == []


@pytest.mark.parametrize("kind", ["notifications", "jobs", "", "feed"])
async def test_glance_page_refuses_every_other_kind(kind):
    page = FakePage()
    browsing, navigator, _ = _browsing(page)
    with pytest.raises(ValueError):
        await browsing.glance_page(kind)
    assert page.log == [] and navigator.visits == []


# --- open_profile_via_search ------------------------------------------------------


def _search_page() -> FakePage:
    page = FakePage()
    page.elements[browsing_module._SEARCH_BOX] = [FakeElement(_box(10), name="search")]
    return page


async def test_a_typeahead_match_is_clicked_and_lands_on_the_profile():
    page = _search_page()
    page.elements[browsing_module._TYPEAHEAD] = [FakeElement(_box(60))]
    target = "https://www.linkedin.com/in/ada-lovelace/"
    page.elements[browsing_module._TYPEAHEAD_PROFILE_LINKS] = [
        FakeElement(_box(60), href="/in/someone-else/", name="other"),
        FakeElement(
            _box(100),
            href="/in/Ada-Lovelace/",
            on_click=lambda: setattr(page, "url", target),
            name="ada",
        ),
    ]
    browsing, navigator, _ = _browsing(page)
    assert await browsing.open_profile_via_search("Ada Lovelace", "ada-lovelace")
    assert "".join(page.typed) == "Ada Lovelace"  # typed one key at a time
    assert len(page.typed) == len("Ada Lovelace")
    assert _clicks(page)[-1] == "ada"
    assert ("press", "Enter") not in page.log
    assert navigator.visits == []


async def test_the_people_results_are_used_when_the_typeahead_has_no_match():
    page = _search_page()
    target = "https://www.linkedin.com/in/ada-lovelace/"

    def show_results() -> None:
        page.url = "https://www.linkedin.com/search/results/all/?keywords=Ada"
        page.elements[browsing_module._RESULT_PROFILE_LINKS] = [
            FakeElement(_box(200), href="/in/ada-l-2/", name="wrong"),
            FakeElement(
                _box(260),
                href="https://www.linkedin.com/in/ada-lovelace?miniProfileUrn=x",
                on_click=lambda: setattr(page, "url", target),
                name="ada",
            ),
        ]

    page.on_enter = show_results
    browsing, navigator, _ = _browsing(page)
    assert await browsing.open_profile_via_search("Ada Lovelace", "ada-lovelace")
    assert ("press", "Enter") in page.log
    assert _clicks(page)[-1] == "ada"
    assert navigator.visits == []


async def test_no_match_returns_false_without_navigating_anywhere_else():
    page = _search_page()

    def show_results() -> None:
        page.url = "https://www.linkedin.com/search/results/all/?keywords=Ada"
        page.elements[browsing_module._RESULT_PROFILE_LINKS] = [
            FakeElement(_box(200), href="/in/somebody/", name="somebody")
        ]

    page.on_enter = show_results
    browsing, navigator, _ = _browsing(page)
    assert (
        await browsing.open_profile_via_search("Ada Lovelace", "ada-lovelace") is False
    )
    assert navigator.visits == []  # pm-platform falls back to the direct URL
    assert "somebody" not in _clicks(page)
    assert page.url.startswith("https://www.linkedin.com/search/results/")


async def test_no_search_box_returns_false_and_types_nothing():
    page = FakePage()
    browsing, navigator, _ = _browsing(page)
    assert (
        await browsing.open_profile_via_search("Ada Lovelace", "ada-lovelace") is False
    )
    assert page.typed == [] and navigator.visits == [] and page.log == []


async def test_the_tripwire_stops_typing_mid_name():
    page = _search_page()

    def trip_after_three(count: int) -> None:
        if count == 3:
            sw.TRIPWIRE.trip(sw.CHECK, "response-999", "https://www.linkedin.com/x")

    page.on_type = trip_after_three
    browsing, navigator, _ = _browsing(page)
    with pytest.raises(SecurityCheckError):
        await browsing.open_profile_via_search("Ada Lovelace", "ada-lovelace")
    assert len(page.typed) == 3
    assert ("press", "Enter") not in page.log
    assert navigator.visits == []


async def test_a_bad_username_is_refused_before_any_input():
    page = _search_page()
    browsing, _, _ = _browsing(page)
    with pytest.raises(ValueError):
        await browsing.open_profile_via_search("Ada", '"]a[href')
    assert page.log == [] and page.typed == []


async def test_the_search_path_is_logged_without_the_typed_name():
    events: list[dict] = []
    activity_log.set_activity_sink(events.append)
    try:
        page = _search_page()
        target = "https://www.linkedin.com/in/ada-lovelace/"
        page.elements[browsing_module._TYPEAHEAD] = [FakeElement(_box(60))]
        page.elements[browsing_module._TYPEAHEAD_PROFILE_LINKS] = [
            FakeElement(
                _box(100),
                href="/in/ada-lovelace/",
                on_click=lambda: setattr(page, "url", target),
            )
        ]
        browsing, _, _ = _browsing(page)
        assert await browsing.open_profile_via_search("Ada Lovelace", "ada-lovelace")
    finally:
        activity_log.set_activity_sink(None)
    steps = [e["step"] for e in events]
    assert "type" in steps and "navigate" in steps
    assert "Lovelace" not in repr([e for e in events if e["step"] == "type"])
