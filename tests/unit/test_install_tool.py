from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install-tool.sh"
CONTENT = b"#!/bin/sh\necho fake tool\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install(bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"PATH": os.environ["PATH"], "BIN_DIR": str(bin_dir)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    path = tmp_path / "artifact"
    path.write_bytes(CONTENT)
    return path


def test_installs_and_stamps(tmp_path: Path, artifact: Path) -> None:
    bin_dir = tmp_path / "bin"

    result = install(bin_dir, "tool", artifact.as_uri(), sha256(CONTENT))

    assert result.returncode == 0, result.stderr
    assert (bin_dir / "tool").read_bytes() == CONTENT
    assert os.access(bin_dir / "tool", os.X_OK)
    assert (bin_dir / ".stamps" / "tool").read_text().strip() == sha256(CONTENT)


def test_second_run_downloads_nothing(tmp_path: Path, artifact: Path) -> None:
    bin_dir = tmp_path / "bin"
    install(bin_dir, "tool", artifact.as_uri(), sha256(CONTENT))
    unreachable = (tmp_path / "missing").as_uri()

    result = install(bin_dir, "tool", unreachable, sha256(CONTENT))

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_checksum_mismatch_leaves_no_binary(tmp_path: Path, artifact: Path) -> None:
    bin_dir = tmp_path / "bin"
    install(bin_dir, "tool", artifact.as_uri(), sha256(CONTENT))

    result = install(bin_dir, "tool", artifact.as_uri(), "0" * 64)

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not (bin_dir / "tool").exists()
    assert not (bin_dir / ".stamps" / "tool").exists()


def test_extracts_tar_member(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("tool")
        info.size = len(CONTENT)
        tar.addfile(info, io.BytesIO(CONTENT))
    archive = tmp_path / "tool.tar.gz"
    archive.write_bytes(buffer.getvalue())
    bin_dir = tmp_path / "bin"

    result = install(bin_dir, "tool", archive.as_uri(), sha256(buffer.getvalue()), "tool")

    assert result.returncode == 0, result.stderr
    assert (bin_dir / "tool").read_bytes() == CONTENT


def test_missing_checksum_fails(tmp_path: Path, artifact: Path) -> None:
    bin_dir = tmp_path / "bin"

    result = install(bin_dir, "tool", artifact.as_uri(), "")

    assert result.returncode == 1
    assert "no pinned SHA-256" in result.stderr
    assert not (bin_dir / "tool").exists()
