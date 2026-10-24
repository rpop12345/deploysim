"""UTC clock and the timestamp formats deploysim writes."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_millis(moment: datetime) -> str:
    """2026-10-22T14:02:26.410Z: audit records and state."""
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def iso_micros(moment: datetime) -> str:
    """Kubernetes MicroTime, used by Lease acquireTime and renewTime."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
