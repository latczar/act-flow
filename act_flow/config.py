"""Settings for the ACT instruction automation demo.

Everything is read from environment variables at call time, so the tests and the
demo can each point the system at their own data folder and their own "today".
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIRM_NAME = "ACT"
ACT_DOMAIN = "act.example"
MAILBOX = f"instructions@{ACT_DOMAIN}"

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".csv"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTEMPTS = 3            # attempts per system call before the case pauses
BREAKER_THRESHOLD = 5       # consecutive failures before a system's circuit opens
BREAKER_SECONDS = 60

# Synthetic staff. Roles decide who may approve what.
USERS = {
    "casey.brown": {"name": "Casey Brown", "title": "Client manager", "roles": ["manager", "reviewer", "security"]},
    "alex.morgan": {"name": "Alex Morgan", "title": "Payroll lead", "roles": ["payroll", "reviewer"]},
    "jordan.lee": {"name": "Jordan Lee", "title": "Compliance officer", "roles": ["compliance", "reviewer"]},
    "robin.shah": {"name": "Robin Shah", "title": "Finance manager", "roles": ["finance", "reviewer"]},
}
DEFAULT_USER = "casey.brown"
ROLE_OWNERS = {"payroll_lead": "alex.morgan", "compliance": "jordan.lee"}
ROLE_LABELS = {
    "manager": "a client manager", "payroll": "the payroll lead", "compliance": "compliance",
    "finance": "finance", "reviewer": "a reviewer", "security": "a client manager (security)",
}


def data_dir() -> Path:
    return Path(os.environ.get("ACT_DATA_DIR") or PROJECT_DIR / "data")


def sub(*parts: str) -> Path:
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def today() -> dt.date:
    value = os.environ.get("ACT_TODAY")
    return dt.date.fromisoformat(value) if value else dt.date.today()


def now_iso() -> str:
    moment = dt.datetime.now(dt.timezone.utc)
    if os.environ.get("ACT_TODAY"):
        d = today()
        moment = moment.replace(year=d.year, month=d.month, day=d.day)
    return moment.isoformat(timespec="seconds")


def retry_delay() -> float:
    return float(os.environ.get("ACT_RETRY_DELAY", "0.5"))


def allow_test_ids() -> bool:
    """Accept HMRC's reserved 'QQ' example prefix for National Insurance numbers."""
    return os.environ.get("ACT_ALLOW_TEST_IDS", "1") == "1"


def extractor_choice() -> str:
    return os.environ.get("ACT_EXTRACTOR", "auto").lower()


def claude_model() -> str:
    return os.environ.get("ACT_CLAUDE_MODEL", "claude-opus-5-5")


def watch_interval() -> float:
    return float(os.environ.get("ACT_WATCH_SECONDS", "5"))


# ---- dates -----------------------------------------------------------------

def add_working_days(start: dt.date, days: int) -> dt.date:
    """Move forward (or back, for negative days) by working days. Bank holidays are ignored in the demo."""
    step = 1 if days >= 0 else -1
    d, remaining = start, abs(days)
    while remaining:
        d += dt.timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def working_days_between(start: dt.date, end: dt.date) -> int:
    count, d = 0, start
    while d < end:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


def next_pay_date(pay_day: int, after: dt.date) -> dt.date:
    """The first pay date on or after a given date, for a monthly payroll paid on `pay_day`."""
    year, month = after.year, after.month
    for _ in range(3):
        candidate = dt.date(year, month, min(pay_day, 28))
        if candidate >= after:
            return candidate
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return candidate


def uk_date(value: str | dt.date | None) -> str:
    if not value:
        return ""
    d = dt.date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d.day} {d:%B %Y}"
