from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from deploysim import __version__

ENTRY_POINTS = {
    "module": [sys.executable, "-m", "deploysim"],
    "console-script": [str(Path(sys.executable).with_name("deploysim"))],
}


@pytest.mark.parametrize("command", ENTRY_POINTS.values(), ids=ENTRY_POINTS.keys())
def test_version(command: list[str]) -> None:
    result = subprocess.run([*command, "--version"], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"deploysim {__version__}"
