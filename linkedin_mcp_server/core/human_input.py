"""Human-paced input for every LinkedIn page action (Shipsmind fork).

LinkedIn scores the ORDER of a member's requests and the TIME BETWEEN them, and
bot-management layers watch for input that no person produces: clicks with no
mouse movement before them, text that arrives in one paste, pages that jump to
the bottom. Fast, evenly timed automation set off security checks on the
operator's account (2026-11-19). Research behind the numbers here:
pm-platform docs/research/linkedin-connector-human-pacing-research-2026-11.md.

Rules this module enforces:

- Every wait is a FRESH RANDOM DRAW. There is no fixed delay and no averaged
  cadence anywhere: the operator's rule is that the pause between actions is a
  random number between 2 and 20 seconds, every time.
- Clicks are real mouse input: a curved path to a random point inside the
  element, a hover, then press and release. Never ``element.click()`` from page
  JavaScript.
- Scrolling is mouse-wheel bursts with reading pauses, never ``scrollTo``.
- Text is typed one key at a time with uneven key timing, pauses at word and
  sentence ends, and the occasional corrected typo. Never ``fill``,
  ``insert_text`` or ``execCommand``.
- Before every input the page is checked for a security check. If one is
  showing, :class:`SecurityCheckError` is raised and NOTHING else is done on
  that page: no click, no key, no scroll, no retry, no navigation. A person
  clears the check in the visible browser window.

Tests switch the waits off with ``LINKEDIN_HUMAN_PACING=0`` (or
:func:`set_pacer_for_testing`); the input events are still produced, so the
tests see the same clicks and keys a real run sends.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import os
import random
import weakref
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from linkedin_mcp_server.core.exceptions import SecurityCheckError  # noqa: F401 - re-exported
from linkedin_mcp_server.core.activity_log import emit
from linkedin_mcp_server.core.security_watch import (
    CHECK,
    TRIPWIRE,
    classify_text,
    is_security_check_url,
    poll_active_page,
    raise_if_tripped,
    set_active_page,
)

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

# Longest stretch any wait sleeps before it looks at the tripwire again, so a
# security check that appears during a pause stops the run within this time.
_TRIPWIRE_SLICE_SECONDS = 0.25

# Ranges every pause is drawn from, fresh each time (never a fixed value).
ACTION_PAUSE_RANGE = (2.0, 20.0)  # operator's rule
PAGE_LOAD_PAUSE_RANGE = (15.0, 45.0)
SETTLE_AFTER_LOAD_RANGE = (1.0, 3.0)
READ_PAUSE_CLAMP = (4.0, 45.0)
PAGE_LOAD_PAUSE_MAX = PAGE_LOAD_PAUSE_RANGE[1]


# --- security checks -------------------------------------------------------


async def raise_if_security_check(page: Any) -> None:
    """Raise if the tripwire fired or *page* is showing a security check.

    Reads only: the tripwire, the page address, every frame address and, on a
    page with no main content, the visible wording. It never clicks, types,
    scrolls or navigates.
    """
    raise_if_tripped()
    set_active_page(page)
    url = str(getattr(page, "url", "") or "")
    if is_security_check_url(url):
        TRIPWIRE.trip(CHECK, "url-checkpoint", url)
        raise_if_tripped()
    try:
        frames = list(getattr(page, "frames", None) or [])
    except TypeError:
        frames = []
    for frame in frames:
        frame_url = str(getattr(frame, "url", "") or "")
        if frame_url and is_security_check_url(frame_url):
            TRIPWIRE.trip(CHECK, "frame-challenge-url", frame_url)
            raise_if_tripped()
    text = await _bare_page_text(page)
    found = classify_text(text)
    if found:
        TRIPWIRE.trip(found[0], found[1] + "-bare-page", url)
        raise_if_tripped()
    if pacer().enabled:  # tests run without the live DOM/cookie poll
        await poll_active_page(force=True)
        raise_if_tripped()


async def _bare_page_text(page: Any) -> str | None:
    """Body text of a page with no <main> (where challenge wording lives)."""
    if getattr(page, "locator", None) is None:
        return None
    try:
        if await page.locator("main").count() > 0:
            return None
        text = await page.locator("body").inner_text(timeout=1500)
    except Exception:  # noqa: BLE001 - reading is best effort
        return None
    return text if isinstance(text, str) else None


# --- pacing ----------------------------------------------------------------


def _pacing_enabled_from_env() -> bool:
    return os.environ.get("LINKEDIN_HUMAN_PACING", "1").strip() not in {
        "0",
        "false",
        "off",
    }


async def _real_sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


@dataclass
class HumanPacer:
    """Draws every wait fresh. ``enabled=False`` keeps the input, drops the waits."""

    rng: random.Random = field(default_factory=random.SystemRandom)
    sleep: Sleep = _real_sleep
    enabled: bool = field(default_factory=_pacing_enabled_from_env)
    # Each session moves at its own speed, like a person on a given day.
    session_speed: float = 1.0

    def __post_init__(self) -> None:
        self.session_speed = self.rng.uniform(0.8, 1.25)
        self._last: float | None = None

    # Draws ------------------------------------------------------------------

    def _fresh(self, low: float, high: float) -> float:
        """A uniform draw in [low, high] that never repeats the previous draw."""
        value = self.rng.uniform(low, high)
        if self._last is not None and abs(value - self._last) < 1e-9:
            value = self.rng.uniform(low, high)
        self._last = value
        return value

    def draw_action_pause(self) -> float:
        """Between UI actions: random 2-20 s (operator's rule)."""
        return self._fresh(*ACTION_PAUSE_RANGE)

    def draw_page_load_pause(self) -> float:
        """Before loading another LinkedIn page: random 15-45 s."""
        return self._fresh(*PAGE_LOAD_PAUSE_RANGE)

    def draw_settle_after_load(self) -> float:
        """After a page appears, before the first input: random 1-3 s."""
        return self._fresh(*SETTLE_AFTER_LOAD_RANGE)

    def draw_read_pause(self, text_len: int) -> float:
        """Time to skim *text_len* characters: ~238 wpm over 20-30% of words."""
        words = max(0, text_len) / 5.5
        share = self.rng.uniform(0.2, 0.3)
        wpm = self.rng.uniform(190.0, 290.0)
        seconds = words * share / (wpm / 60.0) * self.session_speed
        seconds += self.rng.uniform(1.0, 4.0)  # finding the place to start
        low, high = READ_PAUSE_CLAMP
        return max(low, min(high, seconds))

    # Waits ------------------------------------------------------------------

    async def wait(self, seconds: float) -> float:
        """Sleep *seconds* in short slices, stopping the moment the tripwire fires."""
        raise_if_tripped()
        if self.enabled and seconds > 0:
            left = seconds
            while left > 0:
                step = min(left, _TRIPWIRE_SLICE_SECONDS)
                await self.sleep(step)
                left -= step
                raise_if_tripped()
                # Polling fallback for the DOM observer and the cookie check.
                await poll_active_page()
                raise_if_tripped()
        return seconds

    async def _logged_pause(self, kind: str, planned: float) -> float:
        started = time.monotonic()
        try:
            return await self.wait(planned)
        finally:
            emit(
                "pause",
                kind=kind,
                planned_s=round(planned, 3),
                actual_s=round(time.monotonic() - started, 3),
            )

    async def action_pause(self) -> float:
        return await self._logged_pause("action", self.draw_action_pause())

    async def page_load_pause(self) -> float:
        return await self._logged_pause("page_load", self.draw_page_load_pause())

    async def settle_after_load(self) -> float:
        return await self._logged_pause("settle", self.draw_settle_after_load())

    async def read_pause(self, text_len: int) -> float:
        return await self._logged_pause("read", self.draw_read_pause(text_len))

    async def short(self, low: float, high: float) -> float:
        """A small random wait inside one gesture (ms-scale steps, holds)."""
        return await self.wait(self.rng.uniform(low, high))


_pacer: HumanPacer | None = None


def pacer() -> HumanPacer:
    global _pacer
    if _pacer is None:
        _pacer = HumanPacer()
    return _pacer


def set_pacer_for_testing(value: HumanPacer | None) -> None:
    global _pacer
    _pacer = value


async def action_pause() -> float:
    return await pacer().action_pause()


async def page_load_pause() -> float:
    return await pacer().page_load_pause()


async def settle_after_load() -> float:
    return await pacer().settle_after_load()


async def read_pause(text_len: int) -> float:
    return await pacer().read_pause(text_len)


# --- mouse -----------------------------------------------------------------

_POINTER: "weakref.WeakKeyDictionary[Any, tuple[float, float]]" = (
    weakref.WeakKeyDictionary()
)


def _viewport(page: Any) -> tuple[float, float]:
    size = getattr(page, "viewport_size", None) or {}
    try:
        return float(size.get("width") or 1280), float(size.get("height") or 800)
    except AttributeError:
        return 1280.0, 800.0


def _pointer(page: Any, rng: random.Random) -> tuple[float, float]:
    try:
        found = _POINTER.get(page)
    except TypeError:
        found = None
    if found is not None:
        return found
    width, height = _viewport(page)
    return rng.uniform(width * 0.3, width * 0.7), rng.uniform(
        height * 0.3, height * 0.7
    )


def _remember_pointer(page: Any, x: float, y: float) -> None:
    try:
        _POINTER[page] = (x, y)
    except TypeError:
        pass


def _min_jerk(t: float) -> float:
    return 10 * t**3 - 15 * t**4 + 6 * t**5


def bezier_path(
    start: tuple[float, float], end: tuple[float, float], rng: random.Random
) -> list[tuple[float, float]]:
    """Points along a gently curved path, spaced by minimum-jerk easing."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    bow = rng.uniform(0.05, 0.18) * distance * rng.choice((-1, 1))
    # Control point off the straight line, at a random point along it.
    along = rng.uniform(0.3, 0.7)
    nx, ny = (-dy / distance, dx / distance) if distance else (0.0, 0.0)
    cx, cy = x0 + dx * along + nx * bow, y0 + dy * along + ny * bow
    steps = max(8, min(60, int(distance / rng.uniform(12, 22)) + rng.randint(6, 14)))
    points = []
    for i in range(1, steps + 1):
        t = _min_jerk(i / steps)
        bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t**2 * x1
        by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t**2 * y1
        points.append((bx + rng.uniform(-0.6, 0.6), by + rng.uniform(-0.6, 0.6)))
    points[-1] = (x1, y1)
    return points


async def move_mouse_to(page: Any, x: float, y: float) -> None:
    """Move the real pointer to (x, y) along a curved, eased path."""
    p = pacer()
    rng = p.rng
    start = _pointer(page, rng)
    target = (x, y)
    # Sometimes land a little past the target, then correct, like a hand does.
    if rng.random() < 0.2:
        over = rng.uniform(4, 14)
        dx, dy = x - start[0], y - start[1]
        dist = math.hypot(dx, dy) or 1.0
        overshoot = (x + dx / dist * over, y + dy / dist * over)
        legs = [overshoot, target]
    else:
        legs = [target]
    current = start
    started = time.monotonic()
    steps = 0
    for leg in legs:
        path = bezier_path(current, leg, rng)
        steps += len(path)
        # Fitts-like move time 0.35-1.2 s spread over the steps, unevenly.
        total = rng.uniform(0.35, 1.2) * p.session_speed
        weights = [rng.uniform(0.6, 1.4) for _ in path]
        scale = total / sum(weights)
        for (px, py), w in zip(path, weights):
            raise_if_tripped()
            await page.mouse.move(px, py)
            await p.wait(w * scale)
        current = leg
    _remember_pointer(page, x, y)
    emit("mouse_move", steps=steps, duration_s=round(time.monotonic() - started, 3))


async def _box(target: Any) -> dict[str, float] | None:
    getter = getattr(target, "bounding_box", None)
    if getter is None:
        return None
    try:
        return await getter()
    except Exception:  # noqa: BLE001
        return None


async def _bring_into_view(page: Any, target: Any) -> dict[str, float] | None:
    """Wheel-scroll until *target* is on screen; answer its box."""
    box = await _box(target)
    _, height = _viewport(page)
    for _ in range(12):
        if box is None:
            break
        top, bottom = box["y"], box["y"] + box["height"]
        if top >= 40 and bottom <= height - 40:
            return box
        direction = 1 if top > height - 40 else -1
        notches = max(1, min(5, int(abs(top - height / 2) / 100)))
        await wheel_burst(page, direction * notches)
        box = await _box(target)
    if box is None:
        return None
    # Last resort for an element the wheel cannot reach (inside a fixed panel).
    scroll = getattr(target, "scroll_into_view_if_needed", None)
    if scroll is not None:
        try:
            await scroll()
        except Exception:  # noqa: BLE001
            pass
        box = await _box(target)
    return box


async def move_and_click(page: Any, target: Any) -> None:
    """Move the mouse to a random point in *target*, hover, press, release."""
    await raise_if_security_check(page)
    p = pacer()
    rng = p.rng
    box = await _bring_into_view(page, target)
    if box is None:
        # No geometry (a test double, or a detached node): use the element's
        # own trusted Playwright click, which still sends real mouse events.
        await target.click()
        return
    x = box["x"] + box["width"] * rng.uniform(0.25, 0.75)
    y = box["y"] + box["height"] * rng.uniform(0.3, 0.7)
    await move_mouse_to(page, x, y)
    await p.short(0.1, 0.4)  # hover
    await raise_if_security_check(page)
    await page.mouse.down()
    held = await p.short(0.07, 0.16)  # press held
    await page.mouse.up()
    emit("click", hold_ms=round(held * 1000))


# --- scrolling --------------------------------------------------------------


async def wheel_burst(page: Any, notches: int) -> None:
    """One burst of wheel notches (~100 px each, uneven) in one direction."""
    p = pacer()
    rng = p.rng
    direction = 1 if notches >= 0 else -1
    moved = 0.0
    for _ in range(abs(notches)):
        raise_if_tripped()
        delta = direction * rng.uniform(85, 120)
        await page.mouse.wheel(0, delta)
        moved += delta
        await p.short(0.03, 0.09)
    emit("wheel_burst", notches=notches, px=round(moved))


async def wheel_scroll(
    page: Any,
    *,
    to_bottom: bool = False,
    max_bursts: int = 40,
    read_text_len: int | None = None,
) -> None:
    """Scroll the page with the mouse wheel in bursts, with reading pauses.

    With ``to_bottom`` it keeps going until the page stops growing (lazy
    loading) or ``max_bursts`` is used. Otherwise it stops at a random point,
    usually within two or three screens, as most readers do.
    """
    await raise_if_security_check(page)
    p = pacer()
    rng = p.rng
    width, height = _viewport(page)
    # Put the pointer over the content first; the wheel scrolls what is under it.
    await move_mouse_to(
        page,
        rng.uniform(width * 0.35, width * 0.65),
        rng.uniform(height * 0.35, height * 0.7),
    )
    screens_wanted = rng.uniform(1.5, 3.2)
    scrolled = 0.0
    still = 0
    previous_height = await _scroll_height(page)
    for _ in range(max_bursts):
        await raise_if_security_check(page)
        notches = rng.randint(1, 5)
        await wheel_burst(page, notches)
        scrolled += notches * 100
        if rng.random() < 0.12:
            back = rng.randint(1, 2)
            await p.short(0.3, 1.2)
            await wheel_burst(page, -back)
            scrolled -= back * 100
        if read_text_len:
            await p.wait(min(p.draw_read_pause(read_text_len) / 4, 8.0))
        else:
            await p.short(0.8, 4.0)
        if not to_bottom and scrolled >= screens_wanted * height:
            return
        current_height = await _scroll_height(page)
        at_bottom = await _at_bottom(page)
        if to_bottom and at_bottom:
            if current_height == previous_height:
                still += 1
                if still >= 2:
                    return
            else:
                still = 0
        previous_height = current_height


async def wheel_over(page: Any, box: dict[str, float], *, direction: int = 1) -> None:
    """Scroll one inner region (a list or panel) with the wheel, like a person.

    Moves the pointer to a random point inside *box*, then sends one or two
    bursts of wheel notches with a random pause after each.
    """
    await raise_if_security_check(page)
    p = pacer()
    rng = p.rng
    x = box["x"] + box["width"] * rng.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * rng.uniform(0.3, 0.7)
    await move_mouse_to(page, x, y)
    for _ in range(rng.randint(1, 2)):
        await wheel_burst(page, (1 if direction >= 0 else -1) * rng.randint(2, 5))
        await p.short(0.8, 4.0)


async def _scroll_height(page: Any) -> int:
    try:
        return int(await page.evaluate("document.body.scrollHeight") or 0)
    except Exception:  # noqa: BLE001
        return 0


async def _at_bottom(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                "window.innerHeight + window.scrollY >= document.body.scrollHeight - 4"
            )
        )
    except Exception:  # noqa: BLE001
        return True


# --- typing ----------------------------------------------------------------

_NEIGHBOURS = {
    "a": "sq",
    "b": "vn",
    "c": "xv",
    "d": "sf",
    "e": "wr",
    "f": "dg",
    "g": "fh",
    "h": "gj",
    "i": "uo",
    "j": "hk",
    "k": "jl",
    "l": "k",
    "m": "n",
    "n": "bm",
    "o": "ip",
    "p": "o",
    "q": "w",
    "r": "et",
    "s": "ad",
    "t": "ry",
    "u": "yi",
    "v": "cb",
    "w": "qe",
    "x": "zc",
    "y": "tu",
    "z": "x",
}


# A line holding a run of 2+ spaces or an NBSP. Chrome's editable fields rewrite
# such whitespace (space <-> NBSP) unless the whole line arrives in one input,
# which would change the text that is sent. Measured 2026-11-20: even a run
# inserted together with the next character was rewritten.
_EXACT_WHITESPACE_LINE = re.compile("[ \u00a0]{2,}|\u00a0")


def _typing_tokens(text: str) -> list[str]:
    """One key per character, except a line with exact whitespace: one insert."""
    tokens: list[str] = []
    for i, line in enumerate(text.split("\n")):
        if i:
            tokens.append("\n")
        if _EXACT_WHITESPACE_LINE.search(line):
            tokens.append(line)
        else:
            tokens.extend(line)
    return tokens


def _key_delay(rng: random.Random, speed: float) -> float:
    """One inter-key interval: skewed, mostly 120-400 ms."""
    value = rng.lognormvariate(math.log(0.2), 0.45) * speed
    return max(0.06, min(1.2, value))


async def _press_char(page: Any, char: str, newline_key: str = "Shift+Enter") -> None:
    raise_if_tripped()
    if char == "\n":
        if newline_key == "paragraph":
            # A new <p>, as LinkedIn's composer models lines, without Enter
            # (which can submit): a trusted insert of the line break, then the
            # arrow key a person uses to move into the new line. Measured
            # 2026-11-20: the insert alone leaves the caret on the old line.
            await page.keyboard.insert_text("\n")
            raise_if_tripped()
            await page.keyboard.press("ArrowRight")
        else:
            # Shift+Enter by default: a line break that never submits anything.
            await page.keyboard.press(newline_key)
    else:
        await page.keyboard.type(char)


async def human_type(
    page: Any,
    target: Any,
    text: str,
    *,
    typos: bool = True,
    still_focused: Callable[[], Awaitable[bool]] | None = None,
    newline_key: str = "Shift+Enter",
) -> str:
    """Click into *target* and type *text* one key at a time, like a person.

    Answers what was actually typed; see :func:`type_text`.
    """
    await move_and_click(page, target)
    await pacer().short(0.3, 1.2)
    return await type_text(
        page, text, typos=typos, still_focused=still_focused, newline_key=newline_key
    )


async def type_text(
    page: Any,
    text: str,
    *,
    typos: bool = True,
    still_focused: Callable[[], Awaitable[bool]] | None = None,
    newline_key: str = "Shift+Enter",
) -> str:
    """Type *text* like a person; see :func:`_type_text`. Logs counts, never text."""
    stats = {"typos": 0}
    started = time.monotonic()
    typed = ""
    try:
        typed = await _type_text(
            page,
            text,
            typos=typos,
            still_focused=still_focused,
            newline_key=newline_key,
            stats=stats,
        )
        return typed
    finally:
        emit(
            "type",
            chars=len(typed),
            planned_chars=len(text),
            duration_s=round(time.monotonic() - started, 3),
            typos=stats["typos"],
        )


async def _type_text(
    page: Any,
    text: str,
    *,
    typos: bool,
    still_focused: Callable[[], Awaitable[bool]] | None,
    newline_key: str,
    stats: dict[str, int],
) -> str:
    """Type *text* where the caret is, one key at a time, like a person.

    Per-key typing gives the page time to move focus mid-text, which a single
    paste never did. ``still_focused`` is asked before every character: the
    first False stops typing, so no key ever lands in another element. Answers
    exactly what was typed into the field (the whole text, or the prefix typed
    before focus moved), so the caller can remove precisely that.
    """
    p = pacer()
    rng = p.rng
    sentence_start = True
    typed = ""
    for index, char in enumerate(_typing_tokens(text)):
        if still_focused is not None and not await still_focused():
            return typed
        if len(char) > 1 or char == "\u00a0":
            # A line with a run of spaces or an NBSP (see _EXACT_WHITESPACE_LINE):
            # one trusted insert of the line keeps it exactly as written.
            raise_if_tripped()
            await page.keyboard.insert_text(char)
            typed += char
            await p.wait(_key_delay(rng, p.session_speed) + rng.uniform(0.1, 0.4))
            continue
        if index % 12 == 0:
            await raise_if_security_check(page)
        if sentence_start and char.strip() and rng.random() < 0.2:
            await p.wait(rng.uniform(2.0, 8.0))  # thinking about the next sentence
        sentence_start = False
        lower = char.lower()
        if typos and p.enabled and lower in _NEIGHBOURS and rng.random() < 0.015:
            wrong = rng.choice(_NEIGHBOURS[lower])
            stats["typos"] += 1
            raise_if_tripped()
            await page.keyboard.type(wrong if char.islower() else wrong.upper())
            await p.wait(_key_delay(rng, p.session_speed))
            await p.wait(rng.uniform(0.2, 0.7))  # noticing the mistake
            raise_if_tripped()
            if still_focused is not None and not await still_focused():
                return typed + (wrong if char.islower() else wrong.upper())
            await page.keyboard.press("Backspace")
            await p.wait(_key_delay(rng, p.session_speed))
        await _press_char(page, char, newline_key)
        typed += char
        delay = _key_delay(rng, p.session_speed)
        if char == " ":
            delay += rng.uniform(0.1, 0.4)
        elif char in ".!?":
            delay += rng.uniform(0.5, 2.0)
            sentence_start = True
        elif char == "\n":
            delay += rng.uniform(0.6, 2.5)
            sentence_start = True
        await p.wait(delay)
    return typed


async def press_key(page: Any, key: str, target: Any | None = None) -> None:
    """Press one key (Escape, Enter...) after a short random pause.

    Refuses while a security check is showing, so a challenge dialog is never
    dismissed with Escape or submitted with Enter.
    """
    await raise_if_security_check(page)
    p = pacer()
    await p.short(0.25, 0.9)
    raise_if_tripped()
    if target is not None:
        await target.press(key)
    else:
        await page.keyboard.press(key)


async def click_marked(page: Any, run_on_linkedin: Any, mark: str) -> bool:
    """Click the element a read-only page script marked with *mark*.

    The page scripts that used to call ``element.click()`` now only tag the
    element (``data-shipsmind-click``); this clicks it with real mouse input
    and removes the tag. Answers whether a marked element was there.
    """
    locator = page.locator(f'[data-shipsmind-click="{mark}"]').first
    try:
        if await page.locator(f'[data-shipsmind-click="{mark}"]').count() == 0:
            return False
        await move_and_click(page, locator)
        return True
    finally:
        try:
            await run_on_linkedin(
                "(mark) => { for (const el of document.querySelectorAll("
                "'[data-shipsmind-click=\"' + mark + '\"]')) "
                "el.removeAttribute('data-shipsmind-click'); return true; }",
                mark,
            )
        except Exception:  # noqa: BLE001 - the tag is harmless if left
            logger.debug("could not remove click mark %s", mark)


async def clear_and_type(page: Any, target: Any, text: str) -> None:
    """Select what is in *target*, delete it with a key, then type *text*."""
    await move_and_click(page, target)
    p = pacer()
    await p.short(0.2, 0.6)
    raise_if_tripped()
    await page.keyboard.press("Control+A")
    await p.short(0.15, 0.5)
    raise_if_tripped()
    await page.keyboard.press("Backspace")
    await p.short(0.3, 1.0)
    await human_type(page, target, text)
