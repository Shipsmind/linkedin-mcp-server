"""When a LinkedIn page's content is ready, by kind (Shipsmind fork).

Each predicate runs in the page (read-only) and holds once the content a
workflow reads is rendered, so a wait finishes the moment the page is ready
on a fast network and keeps waiting on a slow one, up to the content
deadline. Structural only (tags, roles, hrefs), per the selector policy.

Human pacing comes after this, never instead of it.
"""

from __future__ import annotations


def _text_over(n: int) -> str:
    return (
        "const main = document.querySelector('main'); "
        f"const text = main ? (main.innerText || '').trim().length > {n} : false;"
    )


_BODIES: dict[str, str] = {
    # Any LinkedIn page: its <main> has rendered some text.
    "main": _text_over(0) + " return text;",
    # A profile: the top card's name heading and at least one action.
    "profile": (
        "const main = document.querySelector('main'); if (!main) return false; "
        "const h1 = main.querySelector('h1'); "
        "if (!h1 || !(h1.innerText || '').trim()) return false; "
        "return main.querySelectorAll('button, a[href*=\"/messaging/\"]').length > 0;"
    ),
    # A details or "show all" view: list items, or more than a loading shell.
    "details": _text_over(200)
    + " return !!main && (main.querySelectorAll('li').length > 0 || text);",
    # A people/company sidebar: profile or company links rendered.
    "sidebar": (
        "return document.querySelectorAll("
        '\'aside a[href*="/in/"], aside a[href*="/company/"]\').length > 0;'
    ),
    # The home feed: posts' text rendered.
    "feed": _text_over(200) + " return text;",
    # Messaging: the conversation list attached, or the page's text.
    "inbox": _text_over(100)
    + " return document.querySelectorAll('main li label[aria-label]').length > 0"
    " || text;",
    # One conversation: its messages' text.
    "thread": _text_over(100) + " return text;",
    # Search results: result links or result text.
    "search": _text_over(200) + " return !!main && (main.querySelectorAll("
    '\'a[href*="/in/"], a[href*="/company/"]\').length > 0 || text);',
}

#: kind -> page predicate. The marker comment lets tooling name the wait.
READY_JS: dict[str, str] = {
    kind: f"() => {{ /* page-ready:{kind} */ {body} }}"
    for kind, body in _BODIES.items()
}
