"""Idempotent console logging setup for CLI scripts.

``logging.basicConfig()`` is *supposed* to be a no-op after the first call,
but that guard only looks at the root logger's own handler list — it does
nothing to stop a script from calling it once directly and a library it
imports from calling it again on a differently-named logger, or from a
script's own ``main()`` running twice in the same process (a test suite
importing and invoking it more than once, a REPL re-run). Either produces
the classic symptom: every log line printed two or three times, once per
handler that got attached. ``setup_logging`` avoids that by marking the
logger it configures; a second call against the *same* logger object is a
no-op for handler attachment, even though it still updates the level (so a
caller can cheaply re-invoke this just to raise/lower verbosity without
worrying about handler duplication).

Stdlib-only, no third-party dependency.
"""

from __future__ import annotations

import logging

_CONFIGURED_MARKER = "_shared_tools_logging_configured"

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level: int | str = logging.INFO,
    *,
    fmt: str = DEFAULT_FORMAT,
    datefmt: str = DEFAULT_DATEFMT,
    stream=None,
    logger: logging.Logger | None = None,
) -> logging.Logger:
    """Attach a formatted ``StreamHandler`` to ``logger`` (the root logger by
    default) and set its level, idempotently.

    Calling this again on the *same* logger object always updates the level,
    but only attaches a handler the first time — a script that calls this in
    both its entrypoint and, say, a shared helper it also imports never ends
    up with duplicated log lines. Calling it on a *different* logger (or a
    fresh process) configures independently, same as any other logger.
    """
    target = logger or logging.getLogger()
    target.setLevel(level)

    if getattr(target, _CONFIGURED_MARKER, False):
        return target

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    target.addHandler(handler)
    setattr(target, _CONFIGURED_MARKER, True)
    return target
