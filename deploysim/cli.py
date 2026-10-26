"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

from deploysim import __version__
from deploysim.audit import AuditError, AuditLog, Entry
from deploysim.controller import (
    ENVIRONMENTS,
    FAULTS,
    RELEASES,
    Controller,
    DeployRequest,
    RecoverRequest,
)
from deploysim.errors import EnvironmentProblem
from deploysim.health import rollout_status
from deploysim.kube import Kubectl, KubeError
from deploysim.outcomes import ExitCode
from deploysim.paths import Paths
from deploysim.state import StateStore
from deploysim.validate import ValidationResult, validate_file

__all__ = ["ExitCode", "build_parser", "main"]


class Parser(argparse.ArgumentParser):
    """Exits 64 (EX_USAGE) on usage errors; argparse's default 2 would mean ROLLED_BACK."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(ExitCode.USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    common = Parser(add_help=False)
    common.add_argument(
        "--audit-log", type=Path, help="audit log path (default: .deploysim/audit.jsonl)"
    )
    common.add_argument(
        "--kubeconfig", type=Path, help="simulator kubeconfig (default: .deploysim/kubeconfig)"
    )

    parser = Parser(
        prog="deploysim",
        description="Promote versioned releases through a simulated KWOK environment.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    validate = commands.add_parser(
        "validate", parents=[common], help="validate a release bundle offline (pass 1)"
    )
    validate.add_argument("file", type=Path, metavar="RELEASE_FILE")

    deploy = commands.add_parser("deploy", parents=[common], help="deploy a release bundle")
    deploy.add_argument("file", type=Path, metavar="RELEASE_FILE")
    deploy.add_argument("--env", required=True, choices=ENVIRONMENTS)
    deploy.add_argument(
        "--allow-degraded", action="store_true", help="deploy into a DEGRADED environment"
    )
    deploy.add_argument(
        "--lease-seconds", type=int, default=30, help="lock duration, renewed while running"
    )
    deploy.add_argument(
        "--inject-fault", choices=FAULTS, help="make the target, or target and rollback, unhealthy"
    )

    recover = commands.add_parser(
        "recover", parents=[common], help="resolve an interrupted deployment"
    )
    recover.add_argument("--app", required=True)
    recover.add_argument("--env", required=True, choices=ENVIRONMENTS)
    recover.add_argument(
        "--lease-seconds", type=int, default=30, help="lock duration, renewed while running"
    )

    status = commands.add_parser("status", parents=[common], help="show state and live rollout")
    status.add_argument("--app", required=True)
    status.add_argument("--env", required=True, choices=ENVIRONMENTS)

    history = commands.add_parser(
        "history", parents=[common], help="print audit records, or verify the log"
    )
    history.add_argument("--app")
    history.add_argument("--env", choices=ENVIRONMENTS)
    history.add_argument("--deployment-id")
    history.add_argument(
        "--verify", action="store_true", help="check the hash chain and sequence numbers"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return ExitCode.USAGE

    paths = Paths.from_env()
    audit = AuditLog(args.audit_log or paths.audit_log)
    try:
        if args.command == "validate":
            return validate_command(args.file, paths, audit)
        if args.command == "deploy":
            request = DeployRequest(
                args.file,
                args.env,
                allow_degraded=args.allow_degraded,
                lease_seconds=args.lease_seconds,
                inject_fault=args.inject_fault,
            )
            report = Controller(paths, audit, kubeconfig=args.kubeconfig).deploy(request)
            emit(report.summary)
            return report.outcome.exit_code
        if args.command == "recover":
            recovery = RecoverRequest(args.app, args.env, lease_seconds=args.lease_seconds)
            report = Controller(paths, audit, kubeconfig=args.kubeconfig).recover(recovery)
            emit(report.summary)
            return report.outcome.exit_code
        if args.command == "history":
            return history_command(audit, args.app, args.env, args.deployment_id, args.verify)
        return status_command(args.app, args.env, paths, args.kubeconfig)
    except AuditError as exc:
        print(f"deploysim: audit log: {exc}", file=sys.stderr)
        return ExitCode.ENVIRONMENT_ERROR
    except Exception:
        print("deploysim: unexpected error", file=sys.stderr)
        traceback.print_exc()
        return ExitCode.ENVIRONMENT_ERROR


def validate_command(file: Path, paths: Paths, audit: AuditLog) -> int:
    try:
        result = validate_file(file, paths)
    except EnvironmentProblem as exc:
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


def history_command(
    audit: AuditLog, app: str | None, env: str | None, deployment_id: str | None, verify: bool
) -> int:
    """Records as JSON lines; with --verify, one JSON report and exit 1 if the log was altered."""
    if verify:
        problems = audit.verify()
        records = len(audit.path.read_text().splitlines()) if audit.path.exists() else 0
        emit(
            {
                "audit_log": str(audit.path),
                "records": records,
                "verified": not problems,
                "problems": [asdict(problem) for problem in problems],
            }
        )
        for problem in problems:
            print(f"ALTERED line {problem.line}: {problem.message}", file=sys.stderr)
        return ExitCode.REJECTED if problems else ExitCode.SUCCESS
    for record in audit.records(app=app, environment=env, deployment_id=deployment_id):
        print(json.dumps(record.to_dict(), separators=(",", ":")))
    return ExitCode.SUCCESS


def status_command(app: str, environment: str, paths: Paths, kubeconfig: Path | None) -> int:
    try:
        env = Kubectl.connect(paths, kubeconfig).environment(environment)
        stored = StateStore(env, app).load()
        selector = f"app.kubernetes.io/name={app}"
        releases = env.get_labelled(RELEASES, selector).items()
        deployments = env.get_labelled("deployments", selector).items()
    except (EnvironmentProblem, KubeError) as exc:
        print(f"deploysim: {exc}", file=sys.stderr)
        return ExitCode.ENVIRONMENT_ERROR
    emit(
        {
            "app": app,
            "environment": environment,
            "state": stored.state.to_dict(),
            "releases": [
                {
                    "name": release["metadata"]["name"],
                    "version": release["spec"]["version"],
                    "phase": (release.get("status") or {}).get("phase"),
                    "attempts": (release.get("status") or {}).get("attempts"),
                }
                for release in releases
            ],
            "rollout": [rollout_status(deployment).to_dict() for deployment in deployments],
        }
    )
    return ExitCode.SUCCESS


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
