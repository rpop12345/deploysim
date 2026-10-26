"""The README is the specification: these tests fail when it drifts from the code."""

from __future__ import annotations

import re
from pathlib import Path

from deploysim.cli import build_parser
from deploysim.controller import FAULTS
from deploysim.outcomes import ExitCode, Outcome

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()


def section(heading: str) -> str:
    """Text from heading up to the next heading of the same or higher level."""
    level = heading.split(" ", 1)[0]
    body = README.split(heading + "\n", 1)[1]
    following = re.search(rf"^#{{1,{len(level)}}} ", body, re.MULTILINE)
    return body[: following.start()] if following else body


def test_exit_codes_match() -> None:
    table = section("### Exit codes")
    documented = {int(code) for code in re.findall(r"^\| (\d+) \|", table, re.MULTILINE)}

    assert documented == {int(code) for code in ExitCode}


def test_outcomes_documented() -> None:
    table = section("### Exit codes")

    for outcome in Outcome:
        assert f"`{outcome.value}`" in table, outcome


def test_commands_documented() -> None:
    commands = section("## Commands")
    parser = build_parser()
    subcommands = next(a for a in parser._actions if a.dest == "command").choices

    assert subcommands is not None
    for name in subcommands:
        assert re.search(rf"^deploysim {name}\b", commands, re.MULTILINE), name


def test_faults_documented() -> None:
    faults = section("### Fault injection")

    for fault in FAULTS:
        assert f"| `{fault}` |" in faults, fault
