from __future__ import annotations

from deploysim.bundle import version_key

# Precedence example from the Semantic Versioning 2.0.0 specification, then later releases.
ORDERED = [
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
    "1.0.1",
    "1.1.0",
    "1.10.0",
    "2.0.0",
]


def test_version_ordering() -> None:
    assert sorted(reversed(ORDERED), key=version_key) == ORDERED


def test_equal_versions() -> None:
    assert version_key("1.2.3") == version_key("1.2.3")
    assert version_key("1.2.3-rc.1") < version_key("1.2.3")
