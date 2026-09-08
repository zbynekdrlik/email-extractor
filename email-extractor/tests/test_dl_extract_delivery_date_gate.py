"""#400: delivery-date sanity gate — a delivery date >30 days before or >14 days
after the message's received date sends the document to review with a plain-Slovak
reason naming the read date, the DL number and supplier.

RED tests first (the gate function does not exist yet).
"""
from datetime import UTC, datetime, timedelta

from app.orders import dl_extract


def test_date_two_years_back_triggers_review():
    """The motivating incident: 08.09.2024 on a mail received 08.09.2026."""
    received = datetime(2026, 9, 8, 9, 3, tzinfo=UTC)
    reason = dl_extract.delivery_date_gate("08.09.2024", received)
    assert reason is not None
    assert "08.09.2024" in reason, (
        f"reason must name the read date: {reason!r}")


def test_date_same_as_received_is_ok():
    """A delivery date matching the received date is perfectly normal."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    assert dl_extract.delivery_date_gate("08.09.2026", received) is None


def test_date_10_days_ahead_is_ok():
    """A delivery date 10 days in the future (within 14-day window) is fine."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    future = received + timedelta(days=10)
    date_str = future.strftime("%d.%m.%Y")
    assert dl_extract.delivery_date_gate(date_str, received) is None


def test_date_15_days_ahead_triggers_review():
    """A delivery date 15 days ahead (>14 days) should trigger review."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    future = received + timedelta(days=15)
    date_str = future.strftime("%d.%m.%Y")
    reason = dl_extract.delivery_date_gate(date_str, received)
    assert reason is not None


def test_date_30_days_back_is_ok():
    """A delivery date exactly 30 days before received is still within range."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    past = received - timedelta(days=30)
    date_str = past.strftime("%d.%m.%Y")
    assert dl_extract.delivery_date_gate(date_str, received) is None


def test_date_31_days_back_triggers_review():
    """A delivery date 31 days before received is out of range."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    past = received - timedelta(days=31)
    date_str = past.strftime("%d.%m.%Y")
    reason = dl_extract.delivery_date_gate(date_str, received)
    assert reason is not None


def test_missing_date_returns_none():
    """An empty/missing delivery date is already handled by validate_document's
    existing missing-date check — this gate should not fire for it."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    assert dl_extract.delivery_date_gate("", received) is None
    assert dl_extract.delivery_date_gate(None, received) is None


def test_unparseable_date_returns_none():
    """A date that cannot be parsed (not DD.MM.YYYY) should not crash."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    assert dl_extract.delivery_date_gate("invalid", received) is None


def test_none_received_date_falls_back_to_now():
    """When received_date is None, the gate falls back to now() — a date
    two years back should still trigger."""
    reason = dl_extract.delivery_date_gate("08.09.2024", None)
    assert reason is not None


def test_reason_mentions_received_date():
    """The review reason should mention the received date for context."""
    received = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    reason = dl_extract.delivery_date_gate("08.09.2024", received)
    assert reason is not None
    assert "08.09.2026" in reason, (
        f"reason must mention the received date: {reason!r}")
