"""Human-paced input and the always-on security watch (Shipsmind fork)."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server.core import security_watch as sw
from linkedin_mcp_server.core.exceptions import RateLimitError, SecurityCheckError
from linkedin_mcp_server.core import human_input as hi

PACKAGE = Path(__file__).resolve().parents[1] / "linkedin_mcp_server"


# --- fakes -----------------------------------------------------------------


class FakeMouse:
    def __init__(self, log: list[tuple]) -> None:
        self.log = log

    async def move(self, x: float, y: float) -> None:
        self.log.append(("move", x, y))

    async def down(self) -> None:
        self.log.append(("down",))

    async def up(self) -> None:
        self.log.append(("up",))

    async def wheel(self, dx: float, dy: float) -> None:
        self.log.append(("wheel", dy))


class FakeKeyboard:
    def __init__(self, log: list[tuple]) -> None:
        self.log = log

    async def type(self, text: str) -> None:
        self.log.append(("type", text))

    async def press(self, key: str) -> None:
        self.log.append(("press", key))

    async def insert_text(self, text: str) -> None:
        self.log.append(("insert", text))


class FakeTarget:
    def __init__(self, box: dict[str, float] | None) -> None:
        self.box = box

    async def bounding_box(self) -> dict[str, float] | None:
        return self.box


class FakePage:
    url = "https://www.linkedin.com/feed/"
    viewport_size = {"width": 1280, "height": 800}
    frames: list[Any] = []

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.mouse = FakeMouse(self.log)
        self.keyboard = FakeKeyboard(self.log)
        self.listeners: dict[str, list] = {}
        self.main_frame = _Frame()

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def emit(self, event: str, payload: Any) -> None:
        for handler in self.listeners.get(event, []):
            handler(payload)

    async def evaluate(self, *_a: Any) -> Any:
        return 1000


class Obj:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def pacer():
    sleep = RecordingSleep()
    p = hi.HumanPacer(rng=random.Random(7), sleep=sleep, enabled=True)
    hi.set_pacer_for_testing(p)
    p.sleeps = sleep  # type: ignore[attr-defined]
    yield p
    hi.set_pacer_for_testing(None)


# --- pacing ----------------------------------------------------------------


def test_action_pause_is_a_fresh_draw_between_2_and_20_seconds():
    p = hi.HumanPacer(rng=random.Random(1), enabled=False)
    draws = [p.draw_action_pause() for _ in range(500)]
    assert all(2.0 <= d <= 20.0 for d in draws)
    assert len(set(draws)) == len(draws)  # every draw differs
    assert all(a != b for a, b in zip(draws, draws[1:]))
    assert max(draws) - min(draws) > 15  # spread over the range, not a cadence


def test_page_settle_and_read_pauses_stay_in_their_ranges():
    p = hi.HumanPacer(rng=random.Random(2), enabled=False)
    assert all(15.0 <= p.draw_page_load_pause() <= 45.0 for _ in range(200))
    assert all(1.0 <= p.draw_settle_after_load() <= 3.0 for _ in range(200))
    assert all(4.0 <= p.draw_read_pause(n) <= 45.0 for n in (0, 500, 5000, 50000))
    assert p.draw_read_pause(4000) > p.draw_read_pause(100)


async def test_waits_are_sliced_so_a_trip_stops_a_pause_midway(pacer):
    async def trip_on_third(seconds: float) -> None:
        pacer.sleeps.calls.append(seconds)
        if len(pacer.sleeps.calls) == 3:
            sw.TRIPWIRE.trip(sw.CHECK, "test", "u")

    pacer.sleep = trip_on_third
    with pytest.raises(SecurityCheckError):
        await pacer.wait(10.0)
    assert len(pacer.sleeps.calls) == 3
    assert all(c <= 0.25 for c in pacer.sleeps.calls)


async def test_zero_wait_mode_still_sends_every_input():
    hi.set_pacer_for_testing(hi.HumanPacer(rng=random.Random(3), enabled=False))
    page = FakePage()
    await hi.human_type(
        page,
        FakeTarget({"x": 10, "y": 10, "width": 100, "height": 20}),
        "Hi there.\nBye",
    )
    typed = [e[1] for e in page.log if e[0] == "type"]
    assert typed == list("Hi there.Bye")  # per-character, no typos in zero-wait
    assert ("press", "Shift+Enter") in page.log  # default line break never submits
    assert ("press", "Enter") not in page.log
    assert ("down",) in page.log and ("up",) in page.log


# --- mouse -----------------------------------------------------------------


async def test_click_is_a_curved_mouse_path_then_press_and_release(pacer):
    page = FakePage()
    await hi.move_and_click(
        page, FakeTarget({"x": 600, "y": 400, "width": 80, "height": 30})
    )
    moves = [e for e in page.log if e[0] == "move"]
    assert len(moves) >= 8
    end = moves[-1]
    assert 600 <= end[1] <= 680 and 400 <= end[2] <= 430
    assert page.log[-2:] == [("down",), ("up",)]


def test_bezier_path_bows_off_the_straight_line():
    pts = hi.bezier_path((0.0, 0.0), (400.0, 0.0), random.Random(5))
    assert max(abs(y) for _, y in pts) > 5
    assert pts[-1] == (400.0, 0.0)


async def test_typing_uses_per_key_events_and_corrects_typos(pacer):
    pacer.rng = random.Random(11)
    page = FakePage()
    text = "the quick brown fox jumps over the lazy dog " * 6
    await hi.type_text(page, text)
    events = [e for e in page.log if e[0] in ("type", "press")]
    assert all(len(e[1]) == 1 for e in events if e[0] == "type")
    # Replaying types and Backspaces gives exactly the text.
    out: list[str] = []
    for kind, value in events:
        if kind == "type":
            out.append(value)
        elif value == "Backspace":
            out.pop()
    assert "".join(out) == text
    key_waits = [c for c in pacer.sleeps.calls if c > 0]
    assert len(set(key_waits)) > len(key_waits) // 2  # uneven key timing


async def test_input_stops_mid_gesture_when_the_tripwire_fires(pacer):
    page = FakePage()
    real_move = page.mouse.move
    count = {"n": 0}

    async def move(x: float, y: float) -> None:
        count["n"] += 1
        if count["n"] == 3:
            sw.TRIPWIRE.trip(sw.CHECK, "response-999", "https://www.linkedin.com/x")
        await real_move(x, y)

    page.mouse.move = move
    with pytest.raises(SecurityCheckError) as info:
        await hi.move_and_click(
            page, FakeTarget({"x": 600, "y": 400, "width": 80, "height": 30})
        )
    assert ("down",) not in page.log
    assert info.value.rule == "response-999"
    assert info.value.url == "https://www.linkedin.com/x"


async def test_no_input_at_all_once_tripped(pacer):
    sw.TRIPWIRE.trip(
        sw.WARNING, "response-429", "https://www.linkedin.com/voyager/api/x"
    )
    page = FakePage()
    for action in (
        hi.move_and_click(page, FakeTarget({"x": 1, "y": 1, "width": 9, "height": 9})),
        hi.wheel_scroll(page),
        hi.type_text(page, "x"),
        hi.press_key(page, "Escape"),
        hi.action_pause(),
    ):
        with pytest.raises(RateLimitError):
            await action
    assert page.log == []


# --- security watch: rules -------------------------------------------------


def _response(status: int, url: str, headers: dict[str, str] | None = None) -> Obj:
    return Obj(status=status, url=url, headers=headers or {})


@pytest.mark.parametrize(
    ("status", "url", "headers", "severity"),
    [
        (999, "https://www.linkedin.com/in/x/", None, sw.CHECK),
        (429, "https://www.linkedin.com/voyager/api/graphql?x", None, sw.WARNING),
        (
            200,
            "https://www.linkedin.com/feed/",
            {"cf-mitigated": "challenge"},
            sw.CHECK,
        ),
    ],
)
def test_response_rules(status, url, headers, severity):
    page = FakePage()
    sw.watch_page(page)
    page.emit("response", _response(status, url, headers))
    assert sw.TRIPWIRE.signal is not None
    assert sw.TRIPWIRE.signal.severity == severity


def test_set_cookie_deleting_li_at_trips():
    page = FakePage()
    sw.watch_page(page, signed_in=True)
    page.emit(
        "response",
        _response(
            200,
            "https://www.linkedin.com/feed/",
            {"set-cookie": "li_at=delete me; Expires=Thu, 01 Jan 1970 00:00:00 GMT"},
        ),
    )
    assert sw.TRIPWIRE.signal.rule == "cookie-deleted"


@pytest.mark.parametrize(
    "url",
    [
        "https://client-api.arkoselabs.com/fc/gt2/public_key/X",
        "https://www.google.com/recaptcha/api2/bframe?k=1",
        "https://www.google.com/recaptcha/enterprise/bframe?k=1",
        "https://newassets.hcaptcha.com/captcha/v1/x",
        "https://challenges.cloudflare.com/turnstile/v0/x",
        "https://www.linkedin.com/checkpoint/challenge/AgX",
        "https://www.linkedin.com/uas/consumer-captcha-v2?x",
    ],
)
def test_challenge_requests_trip(url):
    page = FakePage()
    sw.watch_page(page)
    page.emit("request", Obj(url=url))
    assert sw.TRIPWIRE.signal is not None and sw.TRIPWIRE.signal.severity == sw.CHECK


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/checkpoint/challengesV2/AQx",
        "https://www.linkedin.com/challenge/x",
        "https://www.linkedin.com/uas/consumer-email-challenge",
        "https://www.linkedin.com/two-step-verification/",
        "https://www.linkedin.com/psettings/manage/verification",
        "https://www.linkedin.com/checkpoint/login/login-restriction/",
        "https://evil.example.com/",
    ],
)
def test_main_frame_navigation_rules(url):
    page = FakePage()
    sw.watch_page(page, signed_in=False)
    page.emit("framenavigated", _main(page, url))
    assert sw.TRIPWIRE.signal is not None


def _main(page: FakePage, url: str) -> Any:
    frame = page.main_frame
    frame.url = url  # type: ignore[attr-defined]
    return frame


class _Frame:
    pass


def test_the_sign_in_form_under_checkpoint_is_not_a_challenge():
    """/checkpoint/lg/ is LinkedIn's sign-in form (account picker)."""
    assert not sw.is_security_check_url(
        "https://www.linkedin.com/checkpoint/lg/login-submit"
    )
    assert (
        sw.classify_request_url("https://www.linkedin.com/checkpoint/lg/login-submit")
        is None
    )
    found = sw.classify_main_frame_url(
        "https://www.linkedin.com/checkpoint/lg/login-submit", signed_in=True
    )
    assert found == (sw.CHECK, "navigation-signed-out")


def test_signed_out_routes_trip_only_after_sign_in():
    page = FakePage()
    page.main_frame = _Frame()
    sw.watch_page(page, signed_in=False)
    page.emit("framenavigated", _main(page, "https://www.linkedin.com/login"))
    assert sw.TRIPWIRE.signal is None
    sw.reset_watch_for_testing()
    page2 = FakePage()
    page2.main_frame = _Frame()
    sw.watch_page(page2, signed_in=True)
    page2.emit("framenavigated", _main(page2, "https://www.linkedin.com/authwall?x"))
    assert sw.TRIPWIRE.signal.rule == "navigation-signed-out"


def test_attached_challenge_frame_trips():
    page = FakePage()
    sw.watch_page(page)
    page.emit("frameattached", Obj(url="https://client-api.arkoselabs.com/x"))
    assert sw.TRIPWIRE.signal.rule == "frame-challenge-url"


def test_goto_error_with_429_or_999_trips():
    sw.trip_on_navigation_error(
        Exception(
            "net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://www.linkedin.com/in/x"
        ),
        "https://www.linkedin.com/in/x",
    )
    assert sw.TRIPWIRE.signal.severity == sw.WARNING
    sw.reset_watch_for_testing()
    sw.trip_on_navigation_error(
        Exception("status 999"), "https://www.linkedin.com/in/x"
    )
    assert sw.TRIPWIRE.signal.severity == sw.CHECK


def test_dom_binding_reports_trip():
    page = FakePage()
    sw.watch_page(page)
    sw.TRIPWIRE.trip(*("check", "dom-challenge-element"), "", "#captcha-internal")
    assert sw.TRIPWIRE.signal.rule == "dom-challenge-element"


@pytest.mark.parametrize(
    ("text", "severity"),
    [
        ("Let's do a quick security check", sw.CHECK),
        ("Please verify you're a human", sw.CHECK),
        ("Are you a robot?", sw.CHECK),
        ("We've restricted your account temporarily", sw.WARNING),
        ("You've reached the weekly invitation limit", sw.WARNING),
        ("We noticed unusual activity", sw.WARNING),
        ("Use of automated tools is prohibited", sw.WARNING),
    ],
)
def test_text_rules(text, severity):
    assert sw.classify_text(text)[0] == severity


# --- false positives that must NOT trip ------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/recaptcha/api2/anchor?k=1",  # hidden anchor on normal pages
        "https://www.google.com/recaptcha/api.js",
        "https://www.linkedin.com/cdn-cgi/challenge-platform/scripts/jsd/main.js",
        "https://www.linkedin.com/in/someone-who-likes-checkpoints/",
        "https://www.linkedin.com/feed/",
    ],
)
def test_normal_page_resources_do_not_trip(url):
    page = FakePage()
    sw.watch_page(page)
    page.emit("request", Obj(url=url))
    page.emit("frameattached", Obj(url=url))
    page.emit("response", _response(200, url))
    assert sw.TRIPWIRE.signal is None


def test_scan_reads_innertext_not_textcontent():
    js = sw.scan_function_js()
    assert "innerText" in js
    assert "textContent" not in js
    assert "visible(" in js  # hidden iframes and elements are ignored


def test_403_and_other_hosts_do_not_trip():
    page = FakePage()
    sw.watch_page(page)
    page.emit("response", _response(403, "https://www.linkedin.com/voyager/api/x"))
    page.emit("response", _response(999, "https://example.com/"))
    page.emit("response", _response(429, "https://static.licdn.com/x"))
    assert sw.TRIPWIRE.signal is None


def test_raised_error_carries_the_signal():
    sw.TRIPWIRE.trip(
        sw.CHECK, "response-999", "https://www.linkedin.com/in/x", status=999
    )
    with pytest.raises(SecurityCheckError) as info:
        sw.raise_if_tripped()
    assert (info.value.rule, info.value.url, info.value.status) == (
        "response-999",
        "https://www.linkedin.com/in/x",
        999,
    )


def test_reset_clears_the_stop():
    sw.TRIPWIRE.trip(sw.CHECK, "x", "u")
    sw.reset_tripwire()
    sw.raise_if_tripped()


# --- guard rail: no fake input in LinkedIn page code -------------------------

_FORBIDDEN = [
    (re.compile(r"\.click\(\)\s*;"), "JS element.click() in a page script"),
    (re.compile(r"\.fill\("), "Playwright fill() pastes text in one go"),
    (re.compile(r"insert_text\("), "keyboard.insert_text pastes text"),
    (re.compile(r"execCommand\("), "execCommand inserts text by script"),
    (re.compile(r"scrollTo\("), "scrollTo jumps without wheel events"),
    (
        re.compile(r"scrollTop\s*=(?!=)"),
        "scrollTop assignment jumps without wheel events",
    ),
]

# Each entry is (file, rule text, why it is allowed). Keep this list short.
_ALLOWED = {
    (
        "core/utils.py",
        "scrollTop assignment jumps without wheel events",
    ): "job-search rail scroller: jobs are not used by the Shipsmind connector",
}


def test_no_fake_input_in_linkedin_page_code():
    files = sorted((PACKAGE / "linkedin").rglob("*.py")) + [
        PACKAGE / "core" / "utils.py"
    ]
    problems = []
    for path in files:
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "core/human_input.py":
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, why in _FORBIDDEN:
            if pattern.search(text) and (rel, why) not in _ALLOWED:
                line = text[: pattern.search(text).start()].count("\n") + 1
                problems.append(f"{rel}:{line}: {why}")
    assert not problems, "\n".join(problems)


# --- inbox rows are opened one at a time with real input ---------------------


class _RowLocator:
    def __init__(self, page: "_RowPage", position: int) -> None:
        self.page, self.position = page, position

    async def get_attribute(self, _name: str) -> str:
        return self.page.labels[self.position]

    def locator(self, _selector: str) -> "_RowLocator":
        return self

    @property
    def first(self) -> "_RowLocator":
        return self

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 20, "y": 100 + 60 * self.position, "width": 300, "height": 50}


class _RowPage(FakePage):
    def __init__(self, labels: list[str], opens: dict[int, str | None]) -> None:
        super().__init__()
        self.labels, self.opens, self.thread = labels, opens, None

    def locator(self, _selector: str) -> Any:
        page = self

        class _Labels:
            def nth(self, i: int) -> _RowLocator:
                return _RowLocator(page, i)

        return _Labels()


class _RowSession:
    def __init__(self, page: _RowPage) -> None:
        self.page = page
        self.pauses = 0
        self.clicked: list[int] = []

    async def run_on_linkedin(self, _script: str, *_a: Any) -> Any:
        return self.page.thread

    async def delay(self, _seconds: float) -> None:
        # A click lands: the y of the last press tells which row it was.
        downs = [e for e in self.page.log if e[0] == "down"]
        if len(downs) > len(self.clicked):
            moves = [e for e in self.page.log if e[0] == "move"]
            row = int((moves[-1][2] - 100) // 60)
            self.clicked.append(row)
            self.page.thread = self.page.opens.get(row, self.page.thread)

    async def action_pause(self) -> None:
        self.pauses += 1


async def test_inbox_rows_are_opened_with_mouse_clicks_and_pauses_between_them():
    from types import SimpleNamespace

    from linkedin_mcp_server.linkedin.conversations import ConversationReader

    hi.set_pacer_for_testing(hi.HumanPacer(rng=random.Random(4), enabled=False))
    page = _RowPage(["A", "B", "C"], {0: "t-a", 1: "t-b", 2: "t-c"})
    session = _RowSession(page)
    listing = {
        "startThreadId": None,
        "rows": [
            {"ariaLabel": "A", "position": 0, "clickable": True},
            {"ariaLabel": "B", "position": 1, "clickable": True},
            {"ariaLabel": "C", "position": 2, "clickable": True},
        ],
    }
    outcome = await ConversationReader._open_rows_humanly(
        SimpleNamespace(_session=session), listing
    )
    assert [r["threadId"] for r in outcome["rows"]] == ["t-a", "t-b", "t-c"]
    assert session.clicked == [0, 1, 2]
    assert session.pauses == 2  # a person's pause between rows, not before the first
    assert outcome["stoppedAt"] is None


async def test_a_row_that_does_not_open_a_thread_stops_the_scan():
    from types import SimpleNamespace

    from linkedin_mcp_server.linkedin.conversations import ConversationReader

    hi.set_pacer_for_testing(hi.HumanPacer(rng=random.Random(4), enabled=False))
    page = _RowPage(["A", "B", "C"], {0: "t-a"})  # row 1 does not move the page
    session = _RowSession(page)
    listing = {
        "startThreadId": None,
        "rows": [
            {"ariaLabel": "A", "position": 0, "clickable": True},
            {"ariaLabel": "B", "position": 1, "clickable": True},
            {"ariaLabel": "C", "position": 2, "clickable": True},
        ],
    }
    outcome = await ConversationReader._open_rows_humanly(
        SimpleNamespace(_session=session), listing
    )
    assert [r["threadId"] for r in outcome["rows"]] == ["t-a"]
    assert outcome["stoppedAt"] == {"ariaLabel": "B", "position": 1}
    assert session.clicked == [0, 1]  # row C is never clicked
