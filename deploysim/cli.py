"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path
from typing import Any

from deploysim import __version__
from deploysim.audit import AuditError, AuditLog, Entry
from deploysim.paths import Paths
from deploysim.validate import ToolError, ValidationResult, validate_file


class ExitCode(IntEnum):
    SUCCESS = 0
    REJECTED = 1
    ROLLED_BACK = 2
    ROLLBACK_FAILED = 3
    LOCKED = 4
    ENVIRONMENT_ERROR = 5
    USAGE = 64


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--audit-log", type=Path, help="audit log path (default: .deploysim/audit.jsonl)"
    )

    parser = argparse.ArgumentParser(
        prog="deploysim",
        description="Promote versioned releases through a simulated KWOK environment.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    validate = commands.add_parser(
        "validate", parents=[common], help="validate a release bundle offline (pass 1)"
    )
    validate.add_argument("file", type=Path, metavar="RELEASE_FILE")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return ExitCode.SUCCESS

    paths = Paths.from_env()
    audit = AuditLog(args.audit_log or paths.audit_log)
    try:
        return validate_command(args.file, paths, audit)
    except AuditError as exc:
        print(f"deploysim: audit log: {exc}", file=sys.stderr)
        return ExitCode.ENVIRONMENT_ERROR


def validate_command(file: Path, paths: Paths, audit: AuditLog) -> int:
    try:
        result = validate_file(file, paths)
    except ToolError as exc:
        audit.append(
            Entry(
                stage="VALIDATE",
                action="validate_bundle",
                result="ERROR",
                detail={"file": str(file), "error": str(exc)},
            )
        )
        emit({"file": str(file), "result": "ENVIRONMENT_ERROR", "error": str(exc)})
        print(f"deploysim: {exc}", file=sys.stderr)
        return ExitCode.ENVIRONMENT_ERROR

    audit.append(
        Entry(
            stage="VALIDATE",
            action="validate_bundle",
            result="OK" if result.ok else "REJECTED",
            app=result.app,
            cmd=list(result.cmd) if result.cmd else None,
            detail={
                "file": str(file),
                "release": result.bundle.name if result.bundle else None,
                "bundle_digest": result.bundle.digest if result.bundle else None,
                "violations": [v.key for v in result.violations],
            },
        )
    )
    emit(summary(result))
    for violation in result.violations:
        where = f"{violation.document}: " if violation.document else ""
        print(f"REJECTED {where}{violation.key}: {violation.message}", file=sys.stderr)
    return ExitCode.SUCCESS if result.ok else ExitCode.REJECTED


def summary(result: ValidationResult) -> dict[str, Any]:
    return {
        "file": str(result.path),
        "result": "VALID" if result.ok else "REJECTED",
        "release": result.bundle.name if result.bundle else None,
        "bundle_digest": result.bundle.digest if result.bundle else None,
        "violations": [v.to_dict() for v in result.violations],
    }


def emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, indent=2))
