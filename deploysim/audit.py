"""Append-only, hash-chained JSONL audit log."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

GENESIS_HASH = "0" * 64
_TAIL_BLOCK = 4096


class AuditError(Exception):
    """The audit log cannot be read or extended safely."""


@dataclass(frozen=True)
class Entry:
    """One action to record; AuditLog.append adds seq, ts and the hash chain."""

    stage: str
    action: str
    result: str
    deployment_id: str | None = None
    attempt: int | None = None
    app: str | None = None
    environment: str | None = None
    cmd: list[str] | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRecord:
    """One line of the log. Field order is the on-disk key order."""

    seq: int
    ts: str
    deployment_id: str | None
    attempt: int | None
    app: str | None
    environment: str | None
    stage: str
    action: str
    result: str
    cmd: list[str] | None
    detail: dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Problem:
    """A defect found by AuditLog.verify; line is 1-based."""

    line: int
    seq: int | None
    message: str


def chain_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    """sha256(prev_hash + canonical JSON of every field except hash)."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _parse(line: bytes) -> AuditRecord:
    try:
        return AuditRecord(**json.loads(line))
    except (ValueError, TypeError) as exc:
        raise AuditError(f"unreadable record: {exc}") from exc


def _fields(record: AuditRecord) -> dict[str, Any]:
    fields = record.to_dict()
    del fields["hash"]
    return fields


class AuditLog:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _utcnow) -> None:
        self.path = path
        self._clock = clock

    def append(self, entry: Entry) -> AuditRecord:
        """Appends one record under an exclusive lock, then flushes and fsyncs it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                last = self._last_record(f)
                draft = AuditRecord(
                    seq=last.seq + 1 if last else 1,
                    ts=_timestamp(self._clock()),
                    deployment_id=entry.deployment_id,
                    attempt=entry.attempt,
                    app=entry.app,
                    environment=entry.environment,
                    stage=entry.stage,
                    action=entry.action,
                    result=entry.result,
                    cmd=entry.cmd,
                    detail=entry.detail,
                    prev_hash=last.hash if last else GENESIS_HASH,
                    hash="",
                )
                record = replace(draft, hash=chain_hash(draft.prev_hash, _fields(draft)))
                line = json.dumps(record.to_dict(), separators=(",", ":"), ensure_ascii=False)
                f.write(line.encode() + b"\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return record

    def records(
        self,
        *,
        app: str | None = None,
        environment: str | None = None,
        deployment_id: str | None = None,
    ) -> list[AuditRecord]:
        """All records in order, optionally filtered. Raises AuditError on an unreadable line."""
        selected = []
        for number, line in enumerate(self._read_lines()[0], 1):
            try:
                record = _parse(line)
            except AuditError as exc:
                raise AuditError(f"{self.path}:{number}: {exc}") from exc
            if app is not None and record.app != app:
                continue
            if environment is not None and record.environment != environment:
                continue
            if deployment_id is not None and record.deployment_id != deployment_id:
                continue
            selected.append(record)
        return selected

    def verify(self) -> list[Problem]:
        """Checks every line for readability, gap-free seq, prev_hash linkage and its own hash."""
        lines, complete = self._read_lines()
        problems: list[Problem] = []
        expected_seq, prev_hash = 1, GENESIS_HASH
        for number, line in enumerate(lines, 1):
            if number == len(lines) and not complete:
                problems.append(Problem(number, None, "incomplete record: no trailing newline"))
                break
            try:
                record = _parse(line)
            except AuditError as exc:
                problems.append(Problem(number, None, str(exc)))
                continue
            if record.seq != expected_seq:
                problems.append(Problem(number, record.seq, f"expected seq {expected_seq}"))
            if record.prev_hash != prev_hash:
                problems.append(
                    Problem(number, record.seq, "prev_hash does not match the previous record")
                )
            if record.hash != chain_hash(record.prev_hash, _fields(record)):
                problems.append(Problem(number, record.seq, "hash mismatch: record was modified"))
            expected_seq, prev_hash = record.seq + 1, record.hash
        return problems

    def _read_lines(self) -> tuple[list[bytes], bool]:
        """Lines without their newlines, and whether the file ends with a newline."""
        if not self.path.exists():
            return [], True
        with self.path.open("rb") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = f.read()
        if not data:
            return [], True
        complete = data.endswith(b"\n")
        lines = data.split(b"\n")
        return (lines[:-1] if complete else lines), complete

    def _last_record(self, f: IO[bytes]) -> AuditRecord | None:
        end = f.seek(0, os.SEEK_END)
        if end == 0:
            return None
        f.seek(end - 1)
        if f.read(1) != b"\n":
            raise AuditError(
                f"{self.path}: last record is incomplete; run deploysim history --verify"
            )
        buf, pos = b"", end - 1
        while pos > 0:
            step = min(_TAIL_BLOCK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            newline = buf.rfind(b"\n")
            if newline != -1:
                buf = buf[newline + 1 :]
                break
        try:
            return _parse(buf)
        except AuditError as exc:
            raise AuditError(f"{self.path}: last record: {exc}") from exc
