"""Opt-in crash reports (Sentry). Off unless BOTH a DSN is configured and the user opted in.

* DSN: ``PLANNER_SENTRY_DSN`` env (set at build time for release binaries).
* Opt-in: ``"crash_reports": true`` in ``~/.apartment-planner/config.json`` (toggled in the app) or
  ``PLANNER_CRASH_REPORTS=1``.
No project data, coordinates or drawings are sent: events are scrubbed to the stack trace only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import __version__


def _opted_in() -> bool:
    if os.environ.get("PLANNER_CRASH_REPORTS") == "1":
        return True
    cfg = Path(os.environ.get("PLANNER_HOME", Path.home() / ".apartment-planner")) / "config.json"
    try:
        return bool(json.loads(cfg.read_text()).get("crash_reports"))
    except (OSError, json.JSONDecodeError):
        return False


def _scrub(event, _hint):
    event.pop("request", None)
    event.pop("user", None)
    for ex in (event.get("exception") or {}).get("values", []):
        for fr in (ex.get("stacktrace") or {}).get("frames", []):
            fr.pop("vars", None)
    event.pop("extra", None)
    return event


def init() -> bool:
    dsn = os.environ.get("PLANNER_SENTRY_DSN")
    if not dsn or not _opted_in():
        return False
    try:
        import sentry_sdk
    except ImportError:
        return False
    sentry_sdk.init(dsn=dsn, release=f"apartment-planner@{__version__}", send_default_pii=False,
                    traces_sample_rate=0.0, before_send=_scrub, max_breadcrumbs=20)
    return True
