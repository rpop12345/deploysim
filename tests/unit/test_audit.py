from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from deploysim.audit import GENESIS_HASH, AuditError, AuditLog, Entry

REPO = Path(__file__).resolve().parents[2]
FIXED = datetime(2026, 10, 22, 14, 2, 26, 410987, tzinfo=UTC)
WRITER = """
import sys, time
from pathlib import Path
from deploysim.audit import AuditLog, Entry
path, writer, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
log = AuditLog(path)
(path.parent / f"ready-{writer}").touch()
while not (path.parent / "go").exists():
    time.sleep(0.001)
for n in range(count):
    log.append(Entry(stage="TEST", action="write", result="OK", detail={"writer": writer, "n": n}))
"""


BASE = Entry(
    stage="DEPLOY",
    action="apply",
    result="OK",
    deployment_id="dep-3f9a1c2b7e4d",
    attempt=1,
    app="orders-api",
    environment="staging",
    cmd=["kubectl", "apply", "--server-side"],
)


def entry(n: int = 0, **overrides: Any) -> Entry:
    return replace(BASE, detail={"n": n}, **overrides)


@pytest.fixture
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl", clock=lambda: FIXED)


def fill(log: AuditLog, count: int) -> None:
    for n in range(count):
        log.append(entry(n))


def rewrite(log: AuditLog, edit: Callable[[list[str]], list[str]]) -> None:
    lines = log.path.read_text().splitlines(keepends=True)
    log.path.write_text("".join(edit(lines)))


def test_seq_gap_free(log: AuditLog) -> None:
    fill(log, 100)

    assert [r.seq for r in log.records()] == list(range(1, 101))


def test_first_record_starts_chain(log: AuditLog) -> None:
    first = log.append(entry())

    assert first.seq == 1
    assert first.prev_hash == GENESIS_HASH


def test_chain_verifies(log: AuditLog) -> None:
    fill(log, 20)

    assert log.verify() == []


def test_empty_log(log: AuditLog) -> None:
    assert log.records() == []
    assert log.verify() == []


def test_detects_edit(log: AuditLog) -> None:
    fill(log, 10)

    def edit(lines: list[str]) -> list[str]:
        record = json.loads(lines[4])
        record["result"] = "FAIL"
        lines[4] = json.dumps(record, separators=(",", ":")) + "\n"
        return lines

    rewrite(log, edit)

    problems = log.verify()
    assert [(p.line, p.seq) for p in problems] == [(5, 5)]
    assert "hash mismatch" in problems[0].message


def test_detects_delete(log: AuditLog) -> None:
    fill(log, 10)

    rewrite(log, lambda lines: lines[:4] + lines[5:])

    problems = log.verify()
    assert {p.line for p in problems} == {5}
    assert any("expected seq 5" in p.message for p in problems)
    assert any("prev_hash" in p.message for p in problems)


def test_detects_reorder(log: AuditLog) -> None:
    fill(log, 10)

    rewrite(log, lambda lines: [*lines[:2], lines[3], lines[2], *lines[4:]])

    problems = log.verify()
    assert {p.line for p in problems} >= {3, 4}
    assert any("expected seq 3" in p.message for p in problems)


def test_detects_truncation_mid_line(log: AuditLog) -> None:
    fill(log, 5)
    data = log.path.read_bytes()

    log.path.write_bytes(data[:-10])

    problems = log.verify()
    assert [(p.line, p.message) for p in problems] == [
        (5, "incomplete record: no trailing newline")
    ]


def test_append_refuses_after_incomplete_record(log: AuditLog) -> None:
    fill(log, 3)
    log.path.write_bytes(log.path.read_bytes()[:-10])

    with pytest.raises(AuditError, match="last record is incomplete"):
        log.append(entry())


def test_concurrent_appends(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    writers, per_writer = 8, 50

    processes = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(path), str(w), str(per_writer)])
        for w in range(writers)
    ]
    deadline = time.monotonic() + 30
    while len(list(tmp_path.glob("ready-*"))) < writers and time.monotonic() < deadline:
        time.sleep(0.01)
    (tmp_path / "go").touch()

    assert all(p.wait(timeout=60) == 0 for p in processes)
    log = AuditLog(path)
    records = log.records()
    assert [r.seq for r in records] == list(range(1, writers * per_writer + 1))
    assert log.verify() == []
    for w in range(writers):
        mine = [r.detail["n"] for r in records if r.detail["writer"] == str(w)]
        assert mine == list(range(per_writer))


def test_timestamp_format(tmp_path: Path) -> None:
    fixed = AuditLog(tmp_path / "fixed.jsonl", clock=lambda: FIXED)
    live = AuditLog(tmp_path / "live.jsonl")

    assert fixed.append(entry()).ts == "2026-10-22T14:02:26.410Z"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", live.append(entry()).ts)


def test_record_fields_match_readme(log: AuditLog) -> None:
    readme = (REPO / "README.md").read_text()
    section = readme.split("### Audit log", 1)[1]
    example = json.loads(section.split("```json", 1)[1].split("```", 1)[0])

    record = log.append(entry())

    assert list(record.to_dict()) == list(example)
    on_disk = json.loads(log.path.read_text().splitlines()[0])
    assert list(on_disk) == list(example)


def test_append_fsyncs(log: AuditLog, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)

    fill(log, 3)

    assert len(synced) == 3


def test_records_filter(log: AuditLog) -> None:
    log.append(entry(app="orders-api", environment="dev", deployment_id="dep-a"))
    log.append(entry(app="orders-api", environment="staging", deployment_id="dep-b"))
    log.append(entry(app="billing", environment="dev", deployment_id="dep-c"))

    assert [r.deployment_id for r in log.records(app="orders-api")] == ["dep-a", "dep-b"]
    assert [r.deployment_id for r in log.records(environment="dev")] == ["dep-a", "dep-c"]
    assert [r.seq for r in log.records(deployment_id="dep-c")] == [3]


def test_unreadable_line_raises_on_records(log: AuditLog) -> None:
    fill(log, 3)
    rewrite(log, lambda lines: [lines[0], "not json\n", lines[2]])

    with pytest.raises(AuditError, match=r"audit.jsonl:2: unreadable record"):
        log.records()


def test_unwritable_log_raises_audit_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    log = AuditLog(locked / "audit.jsonl")

    try:
        with pytest.raises(AuditError, match="cannot write"):
            log.append(entry())
    finally:
        locked.chmod(0o700)
