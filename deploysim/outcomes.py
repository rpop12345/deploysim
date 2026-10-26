"""Run outcomes and the exit code each maps to."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    REJECTED = 1
    ROLLED_BACK = 2
    ROLLBACK_FAILED = 3
    LOCKED = 4
    ENVIRONMENT_ERROR = 5
    USAGE = 64


class Outcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_OP = "NO_OP"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    LOCKED = "LOCKED"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"

    @property
    def exit_code(self) -> ExitCode:
        return _EXIT_CODES[self]


_EXIT_CODES = {
    Outcome.SUCCEEDED: ExitCode.SUCCESS,
    Outcome.NO_OP: ExitCode.SUCCESS,
    Outcome.REJECTED: ExitCode.REJECTED,
    Outcome.ROLLED_BACK: ExitCode.ROLLED_BACK,
    Outcome.ROLLBACK_FAILED: ExitCode.ROLLBACK_FAILED,
    Outcome.LOCKED: ExitCode.LOCKED,
    Outcome.ENVIRONMENT_ERROR: ExitCode.ENVIRONMENT_ERROR,
}
