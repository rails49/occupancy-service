"""Whether a browser request came from the page the server is part of.

A copy of ``is_own_page`` from ``control``, with the code unchanged. The rule and why it holds are
in https://github.com/rails49/control/blob/main/docs/adr/0057-one-origin-rule-and-both-faces-read-it.md
"""

from urllib.parse import urlsplit

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
"""Where a page served from this machine is at. A page an attacker controls is
served from somewhere else and its origin is that somewhere, so none can claim
one of these — which is what makes admitting them free."""


def is_own_page(origin: str | None, host: str | None) -> bool:
    """Whether a request carrying this `Origin` and `Host` is the app's own.

    Host against host and not scheme against scheme, because TLS terminates at
    the door: the browser's origin is `https://control.example.org` and what
    arrives is plain HTTP under that same `Host` (ADR-0042).

    A loopback origin is admitted whatever the `Host`, because a page served
    from this machine reaches a face on another port of it, and the ports are
    not one origin: a proxy that rewrites `Host` to the target — which vite
    did for the store's routes until #351 — leaves the two disagreeing.
    Anyone who can serve a page from here can reach the face with no browser
    at all, so the clause gives away nothing the machine did not already have
    (ADR-0057).
    """
    if origin is None:
        return True
    at = urlsplit(origin)
    return at.hostname in LOOPBACK or at.netloc == (host or "")
