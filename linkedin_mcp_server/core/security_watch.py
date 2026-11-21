"""Always-on security watch for the LinkedIn browser (Shipsmind fork).

Goal: stop BEFORE LinkedIn locks the account. A watcher is installed once on
the browser context and listens to everything the browser sends and receives.
The first hit trips a process-wide TRIPWIRE that every human-input primitive
(each mouse step, key, wheel notch, pause slice and navigation) checks, so an
action in flight halts mid-way: no further input, no goto/reload/close/back,
no retry, no dismissing anything. A person clears a security check in the
visible window; the operator's tooling then calls :func:`reset_tripwire`.

Signals, earliest first (research: stickerdaniel/linkedin-mcp-server PRs #1264,
#957, #244; offerfinch detect.ts; linki session.ts; Cloudflare cf-mitigated;
joeyism #277 on textContent false positives; Patchright README):

1. responses: HTTP 999 (check) or 429 (warning) from any *.linkedin.com host,
   XHR/fetch included; ``cf-mitigated: challenge``; Set-Cookie deleting li_at;
   failed LinkedIn document requests and goto errors carrying 429/999;
2. requests to challenge providers / checkpoint URLs (any resource type);
3. main-frame navigation to checkpoint / challenge / sign-in / verification
   routes or off LinkedIn; frames attached from challenge providers;
4. a read-only DOM scan, polled (page.evaluate) from every human-input wait and
   before every input, for visible challenge iframes/forms and for security or
   limit wording in LinkedIn notices and the document title - innerText only,
   never textContent. (Not an init-script MutationObserver: under Patchright an
   init script or exposed binding stops cross-origin iframes loading.)
5. the li_at cookie disappearing or changing.

Severities: ``check`` -> :class:`SecurityCheckError` (wait for a person);
``warning`` -> :class:`RateLimitError` (precursor; operator tooling pauses
72 h). Both stop all input. The pattern lists below are DATA: extend them here.

Never use a bare ``iframe[src*=captcha]`` test: normal LinkedIn pages carry a
hidden reCAPTCHA anchor and Cloudflare's jsd script (PR #244).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from weakref import WeakSet

from .activity_log import emit
from .exceptions import RateLimitError, SecurityCheckError

logger = logging.getLogger(__name__)

CHECK = "check"
WARNING = "warning"

#: Seconds a warning asks the caller to wait (72 h, per the threshold research).
WARNING_WAIT_SECONDS = 72 * 3600

# --- patterns (data) -------------------------------------------------------

#: Request/frame URLs that only a challenge loads (any resource type).
#: Deliberately NOT the plain reCAPTCHA anchor or api.js, and NOT Cloudflare's
#: /cdn-cgi/challenge-platform/scripts/jsd: both load on normal pages.
CHALLENGE_URL_RE = re.compile(
    r"arkoselabs\.com|funcaptcha|captchaInternal|recaptcha/(?:api2|enterprise)/bframe"
    r"|hcaptcha\.com|challenges\.cloudflare\.com|/checkpoint/(?!lg/)|/uas/consumer-captcha",
    re.IGNORECASE,
)

#: Main-frame paths that are a security check, always.
CHECK_PATH_RE = re.compile(
    # /checkpoint/lg/ is LinkedIn's sign-in form (account picker), not a check.
    r"^/(?:checkpoint(?!/lg/)|challenge|uas/consumer-)"
    r"|/two-step-verification|/manage/verification|login/login-restriction/?$",
    re.IGNORECASE,
)

#: Main-frame paths that mean LinkedIn signed the session out. Only a check
#: once the context is known to have been signed in (not during --login).
SIGNED_OUT_PATH_RE = re.compile(
    r"^/(?:authwall|uas/login|checkpoint/lg/)|/login/?$|/reauthentication",
    re.IGNORECASE,
)

#: Response statuses from LinkedIn hosts.
CHECK_STATUSES: frozenset[int] = frozenset({999})
WARNING_STATUSES: frozenset[int] = frozenset({429})

#: Wording, matched against the innerText of LinkedIn notices and the title.
SECURITY_TEXT_RE = re.compile(
    r"let'?s do a quick security check|security verification"
    r"|verify (?:that )?you'?re (?:a )?human|are you a robot",
    re.IGNORECASE,
)
WARNING_TEXT_RE = re.compile(
    r"we'?ve restricted|(?:account|profile).{0,40}restricted|unusual activity"
    r"|automated (?:activity|tools|software)|too many requests"
    r"|exceeded the (?:daily|weekly) limit|weekly invitation limit|out of invitations",
    re.IGNORECASE,
)

#: Where LinkedIn shows notices; wording is only trusted inside these.
NOTICE_SELECTOR = (
    "[role=alert],.artdeco-toast-item,.artdeco-modal,.artdeco-inline-feedback--error"
)

#: Elements that only a challenge renders (checked only when visible).
CHALLENGE_ELEMENT_SELECTOR = '#captcha-internal,form#captcha-challenge,form[action*="challenge/verify"],[data-pkey]'

# --- signal + tripwire -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    severity: str  # CHECK or WARNING
    rule: str  # which rule matched, e.g. "response-999"
    url: str
    detail: str
    status: int | None
    at: float


class Tripwire:
    """Process-wide stop flag. The first signal wins; a check outranks a warning."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signal: Signal | None = None

    def trip(
        self,
        severity: str,
        rule: str,
        url: str = "",
        detail: str = "",
        status: int | None = None,
    ) -> None:
        with self._lock:
            if self._signal is not None and not (
                self._signal.severity == WARNING and severity == CHECK
            ):
                logger.info(
                    "Further security signal %s (%s) %s", rule, severity, url[:200]
                )
                return
            self._signal = Signal(
                severity, rule, url[:500], detail[:300], status, time.time()
            )
        emit("tripwire", rule=rule, url=url[:500], severity=severity, status=status)
        logger.warning(
            "LinkedIn %s tripped by %s: %s %s", severity, rule, url[:200], detail[:200]
        )

    @property
    def signal(self) -> Signal | None:
        return self._signal

    def tripped(self) -> bool:
        return self._signal is not None

    def reset(self) -> None:
        with self._lock:
            self._signal = None

    def raise_if_tripped(self) -> None:
        signal = self._signal
        if signal is None:
            return
        where = f"{signal.rule} {signal.url}".strip()
        if signal.severity == CHECK:
            error: Exception = SecurityCheckError(where)
        else:
            error = RateLimitError(
                f"LinkedIn warning ({where}). All input stopped.",
                suggested_wait_time=WARNING_WAIT_SECONDS,
            )
        error.url = signal.url  # type: ignore[attr-defined]
        error.rule = signal.rule  # type: ignore[attr-defined]
        error.status = signal.status  # type: ignore[attr-defined]
        error.severity = signal.severity  # type: ignore[attr-defined]
        raise error


TRIPWIRE = Tripwire()


def raise_if_tripped() -> None:
    TRIPWIRE.raise_if_tripped()


def reset_tripwire() -> None:
    """Called once a person has cleared the check (and in tests).

    The remembered session cookie is forgotten too: clearing a check can
    legitimately issue a new li_at, which is learned again on the next poll.
    """
    TRIPWIRE.reset()
    _session_cookie.clear()


def current_signal() -> Signal | None:
    return TRIPWIRE.signal


# --- classifiers -----------------------------------------------------------


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def is_linkedin_host(host: str) -> bool:
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def classify_main_frame_url(
    url: str | None, *, signed_in: bool
) -> tuple[str, str] | None:
    """(severity, rule) for a main-frame navigation, or None."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None  # about:blank, chrome-error:// (a network error, not a check)
    if CHALLENGE_URL_RE.search(url):
        return CHECK, "navigation-challenge-url"
    if not is_linkedin_host((parsed.hostname or "").lower()):
        return CHECK, "navigation-off-linkedin"
    path = parsed.path or "/"
    if CHECK_PATH_RE.search(path):
        return CHECK, "navigation-checkpoint"
    if signed_in and SIGNED_OUT_PATH_RE.search(path):
        return CHECK, "navigation-signed-out"
    return None


def is_security_check_url(url: str | None) -> bool:
    """A checkpoint/challenge address (sign-in routes and off-site excluded)."""
    if not url:
        return False
    if CHALLENGE_URL_RE.search(url):
        return True
    parsed = urlparse(url)
    if not is_linkedin_host((parsed.hostname or "").lower()):
        return False
    return bool(CHECK_PATH_RE.search(parsed.path or "/"))


def classify_request_url(url: str | None) -> str | None:
    return CHECK if url and CHALLENGE_URL_RE.search(url) else None


def classify_response(
    status: int, url: str, headers: dict[str, str] | None
) -> tuple[str, str] | None:
    """(severity, rule) for one response, or None."""
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return CHECK, "response-cf-mitigated"
    if not is_linkedin_host(_host(url)):
        return None
    if status in CHECK_STATUSES:
        return CHECK, f"response-{status}"
    if status in WARNING_STATUSES:
        return WARNING, f"response-{status}"
    return None


def cookie_deletes_session(headers: dict[str, str] | None) -> bool:
    """A Set-Cookie that removes ``li_at`` (LinkedIn signing the session out)."""
    for key, value in (headers or {}).items():
        if str(key).lower() != "set-cookie":
            continue
        for line in str(value).split("\n"):
            low = line.strip().lower()
            if not low.startswith("li_at="):
                continue
            if (
                low.startswith("li_at=delete")
                or low.startswith("li_at=;")
                or low.startswith('li_at="";')
                or "max-age=0" in low
                or "expires=thu, 01 jan 1970" in low
                or "expires=thu, 01-jan-1970" in low
            ):
                return True
    return False


def classify_text(text: str | None) -> tuple[str, str] | None:
    """(severity, rule) for notice/title wording, or None."""
    if not text:
        return None
    if SECURITY_TEXT_RE.search(text):
        return CHECK, "text-security"
    if WARNING_TEXT_RE.search(text):
        return WARNING, "text-warning"
    return None


_STATUS_IN_ERROR_RE = re.compile(r"\b(999|429)\b|ERR_HTTP_RESPONSE_CODE_FAILURE")


def classify_navigation_error(
    exc: BaseException | str, url: str = ""
) -> tuple[str, str] | None:
    """A goto/requestfailed error that is LinkedIn refusing the request (#957)."""
    text = str(exc)
    match = _STATUS_IN_ERROR_RE.search(text)
    if not match:
        return None
    if url and not is_linkedin_host(_host(url)):
        return None
    if match.group(1) == "999":
        return CHECK, "navigation-error-999"
    return WARNING, "navigation-error-refused"


def trip_on_navigation_error(exc: BaseException, url: str = "") -> None:
    found = classify_navigation_error(exc, url)
    if found:
        TRIPWIRE.trip(found[0], found[1], url, str(exc)[:200])


def raise_if_security_check_url(url: str | None) -> None:
    """Trip and raise SecurityCheckError when *url* is a checkpoint/challenge."""
    if is_security_check_url(url):
        TRIPWIRE.trip(CHECK, "url-checkpoint", str(url))
        TRIPWIRE.raise_if_tripped()


# --- the DOM scan (shared by the observer and the polling fallback) ---------

#: Returns {kind, rule, detail} or null. Runs in the page; reads only.
_SCAN_FN_JS = """
() => {
  const urlRe = new RegExp(%(url_re)s, 'i');
  const secRe = new RegExp(%(sec_re)s, 'i');
  const warnRe = new RegExp(%(warn_re)s, 'i');
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
      && (el.offsetParent !== null || s.position === 'fixed');
  };
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.getAttribute('src') || '';
    if (urlRe.test(src) && visible(f)) return {kind: 'check', rule: 'dom-challenge-iframe', detail: src.slice(0, 300)};
  }
  for (const el of document.querySelectorAll(%(elem_sel)s)) {
    if (visible(el)) return {kind: 'check', rule: 'dom-challenge-element', detail: (el.id || el.tagName)};
  }
  const texts = [document.title || ''];
  for (const n of document.querySelectorAll(%(notice_sel)s)) {
    if (visible(n)) texts.push(n.innerText || '');
  }
  for (const t of texts) {
    if (!t) continue;
    if (secRe.test(t)) return {kind: 'check', rule: 'text-security', detail: t.slice(0, 200)};
  }
  for (const t of texts) {
    if (!t) continue;
    if (warnRe.test(t)) return {kind: 'warning', rule: 'text-warning', detail: t.slice(0, 200)};
  }
  return null;
}
"""


def scan_function_js() -> str:
    return _SCAN_FN_JS % {
        "url_re": json.dumps(CHALLENGE_URL_RE.pattern),
        "sec_re": json.dumps(SECURITY_TEXT_RE.pattern),
        "warn_re": json.dumps(WARNING_TEXT_RE.pattern),
        "elem_sel": json.dumps(CHALLENGE_ELEMENT_SELECTOR),
        "notice_sel": json.dumps(NOTICE_SELECTOR),
    }


# --- the watcher -----------------------------------------------------------

_installed_contexts: "WeakSet[Any]" = WeakSet()
_signed_in_contexts: "WeakSet[Any]" = WeakSet()
_watched_pages: "WeakSet[Any]" = WeakSet()
_session_cookie: dict[int, str] = {}
_active_page: list[Any] = []  # the page human input is driving now
_last_poll = [0.0]
_last_cookie_poll = [0.0]

POLL_INTERVAL_SECONDS = 0.5
COOKIE_POLL_INTERVAL_SECONDS = 3.0


def _in(container: Any, item: Any) -> bool:
    try:
        return item in container
    except TypeError:
        return False


def _add(container: Any, item: Any) -> None:
    try:
        container.add(item)
    except TypeError:
        pass


def _on_response(response: Any, signed_in: bool) -> None:
    try:
        status = int(getattr(response, "status", 0) or 0)
        url = str(getattr(response, "url", "") or "")
        headers = getattr(response, "headers", None) or {}
        if callable(headers) or not isinstance(headers, dict):
            headers = {}
    except Exception:  # noqa: BLE001
        return
    found = classify_response(status, url, headers)
    request = getattr(response, "request", None)
    if getattr(request, "resource_type", None) == "document":
        emit("page_loaded", url=url[:500], status=status)
    if found:
        _signal(found[1], url, status, True)
        TRIPWIRE.trip(found[0], found[1], url, status=status)
        return
    if is_linkedin_host(_host(url)) and status >= 400:
        _signal("http-error-ignored", url, status, False)
    if signed_in and is_linkedin_host(_host(url)) and cookie_deletes_session(headers):
        TRIPWIRE.trip(CHECK, "cookie-deleted", url, "Set-Cookie removed li_at", status)


def _signal(rule: str, url: str, status: int | None, tripped: bool) -> None:
    emit("watch_signal", rule=rule, url=url[:500], status=status, tripped=tripped)


#: Looks like a challenge but is not one (the hidden reCAPTCHA anchor, the
#: Cloudflare jsd script): logged as a near-miss, never tripped.
_NEAR_MISS_RE = re.compile(r"captcha|challenge", re.IGNORECASE)


def _on_request(request: Any) -> None:
    url = str(getattr(request, "url", "") or "")
    if classify_request_url(url):
        _signal("request-challenge-url", url, None, True)
        TRIPWIRE.trip(CHECK, "request-challenge-url", url)
    elif _NEAR_MISS_RE.search(url):
        _signal("near-miss-ignored", url, None, False)


def _on_request_failed(request: Any) -> None:
    url = str(getattr(request, "url", "") or "")
    failure = getattr(request, "failure", None)
    try:
        text = failure() if callable(failure) else failure
    except Exception:  # noqa: BLE001
        text = None
    if text and is_linkedin_host(_host(url)):
        found = classify_navigation_error(str(text), url)
        if found:
            TRIPWIRE.trip(found[0], "requestfailed-" + found[1], url, str(text))


def watch_page(page: Any, *, signed_in: bool = True) -> None:
    """Listen on one page. Safe to call more than once."""
    if _in(_watched_pages, page):
        set_active_page(page)
        return
    _add(_watched_pages, page)
    set_active_page(page)
    on = getattr(page, "on", None)
    if on is None:
        return
    main_frame = getattr(page, "main_frame", None)

    def on_navigated(frame: Any) -> None:
        url = str(getattr(frame, "url", "") or "")
        is_main = main_frame is None or frame is main_frame or frame == main_frame
        if is_main:
            found = classify_main_frame_url(url, signed_in=signed_in)
            if found:
                _signal(found[1], url, None, True)
                TRIPWIRE.trip(found[0], found[1], url)
        elif classify_request_url(url):
            _signal("frame-challenge-url", url, None, True)
            TRIPWIRE.trip(CHECK, "frame-challenge-url", url)
        elif _NEAR_MISS_RE.search(url):
            _signal("near-miss-ignored", url, None, False)

    def on_attached(frame: Any) -> None:
        url = str(getattr(frame, "url", "") or "")
        if classify_request_url(url):
            _signal("frame-challenge-url", url, None, True)
            TRIPWIRE.trip(CHECK, "frame-challenge-url", url)
        elif _NEAR_MISS_RE.search(url):
            _signal("near-miss-ignored", url, None, False)

    try:
        on("response", lambda r: _on_response(r, signed_in))
        on("request", _on_request)
        on("requestfailed", _on_request_failed)
        on("framenavigated", on_navigated)
        on("frameattached", on_attached)
    except Exception:  # noqa: BLE001 - a double without events
        logger.debug("page has no event hooks; the polling fallback still runs")


def set_active_page(page: Any) -> None:
    _active_page[:] = [page]


async def install_security_watch(context: Any, page: Any | None = None) -> None:
    """Install the watch once on *context* (and its current and future pages)."""
    cookie = await _session_cookie_value(context)
    signed_in = bool(cookie)
    if signed_in:
        _add(_signed_in_contexts, context)
        _session_cookie[id(context)] = cookie or ""
    if context is not None and not _in(_installed_contexts, context):
        _add(_installed_contexts, context)

        # No init script and no exposed binding: under Patchright either one
        # alone stops cross-origin iframes from loading (measured 2026-11-20
        # with tests/test_browser_identity.py), and challenge widgets ARE
        # cross-origin iframes. The DOM scan runs as a read-only poll instead
        # (poll_active_page), from every human-input wait and before inputs.
        try:
            context.on("page", lambda p: watch_page(p, signed_in=signed_in))
        except Exception:  # noqa: BLE001
            pass
        try:
            existing = list(getattr(context, "pages", None) or [])
        except TypeError:
            existing = []
        for each in existing:
            watch_page(each, signed_in=signed_in)
    if page is not None:
        watch_page(page, signed_in=signed_in)


async def _session_cookie_value(context: Any) -> str | None:
    cookies = getattr(context, "cookies", None)
    if cookies is None:
        return None
    try:
        found = await cookies("https://www.linkedin.com/")
    except Exception:  # noqa: BLE001
        return None
    for cookie in found or []:
        if (
            isinstance(cookie, dict)
            and cookie.get("name") == "li_at"
            and cookie.get("value")
        ):
            return str(cookie["value"])
    return None


async def poll_active_page(*, force: bool = False) -> None:
    """Polling fallback: scan the active page's DOM and its session cookie.

    Called by every human-input wait slice; throttled to POLL_INTERVAL_SECONDS
    (DOM) and COOKIE_POLL_INTERVAL_SECONDS (cookie). Reads only.
    """
    if TRIPWIRE.tripped() or not _active_page:
        return
    page = _active_page[0]
    now = time.monotonic()
    if force or now - _last_poll[0] >= POLL_INTERVAL_SECONDS:
        _last_poll[0] = now
        evaluate = getattr(page, "evaluate", None)
        if evaluate is not None:
            try:
                hit = await evaluate(scan_function_js())
            except Exception:  # noqa: BLE001 - navigating, closed, a double
                hit = None
            if isinstance(hit, dict) and hit.get("kind") in (CHECK, WARNING):
                TRIPWIRE.trip(
                    str(hit["kind"]),
                    str(hit.get("rule") or "dom-poll"),
                    str(getattr(page, "url", "") or ""),
                    str(hit.get("detail") or ""),
                )
                return
    if force or now - _last_cookie_poll[0] >= COOKIE_POLL_INTERVAL_SECONDS:
        _last_cookie_poll[0] = now
        await _check_session_cookie(page)


async def _check_session_cookie(page: Any) -> None:
    context = getattr(page, "context", None)
    if context is None or not _in(_signed_in_contexts, context):
        return
    value = await _session_cookie_value(context)
    before = _session_cookie.get(id(context))
    if value and not before:
        _session_cookie[id(context)] = value
        return
    if not value:
        TRIPWIRE.trip(CHECK, "cookie-missing", "", "li_at session cookie is gone")
    elif before and value != before:
        TRIPWIRE.trip(CHECK, "cookie-changed", "", "li_at session cookie changed")


def reset_watch_for_testing() -> None:
    TRIPWIRE.reset()
    _installed_contexts.clear()
    _signed_in_contexts.clear()
    _watched_pages.clear()
    _session_cookie.clear()
    _active_page.clear()
    _last_poll[0] = 0.0
    _last_cookie_poll[0] = 0.0
