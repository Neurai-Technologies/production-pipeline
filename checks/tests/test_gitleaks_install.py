"""The Gitleaks install steps download the pinned release asset through
GitHub's API, with the job token, retries and the unchanged SHA-256 check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import LIBRARY_ROOT

WORKFLOWS = [
    LIBRARY_ROOT / ".github" / "workflows" / "ci.yml",
    LIBRARY_ROOT / ".github" / "workflows" / "reusable-ci.yml",
]
STEP = "Install checksum-pinned Gitleaks"
ASSET_URL = "https://api.github.com/repos/gitleaks/gitleaks/releases/assets/378332058"
CHECKSUM_LINE = (
    "printf '%s  %s\\n' "
    "'551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb' "
    '"$target/gitleaks.tar.gz" | sha256sum -c -'
)


def install_step(path: Path) -> dict[str, Any]:
    """Return the one Gitleaks install step of a workflow.

    Args:
        path: The workflow file.

    Returns:
        The step as data.
    """
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    found = [
        step for job in workflow["jobs"].values() for step in job.get("steps", []) if step.get("name") == STEP
    ]
    assert len(found) == 1, f"{path.name}: step {STEP!r} moved; update this test"
    return found[0]


def curl_line(lines: list[str]) -> int:
    """Return the index of the one curl line of a step's script.

    Args:
        lines: The step's `run` lines, stripped.

    Returns:
        The index of the line that starts with curl.
    """
    found = [i for i, line in enumerate(lines) if line.startswith("curl ")]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_install_downloads_the_api_asset_with_the_job_token(path: Path) -> None:
    step = install_step(path)
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    run = step["run"]
    assert "github.token" not in run
    assert "releases/download" not in run
    lines = [line.strip() for line in run.splitlines()]
    curl = lines[curl_line(lines)]
    assert ASSET_URL in curl
    assert "-H 'Accept: application/octet-stream'" in curl
    assert '-H "Authorization: Bearer $GH_TOKEN"' in curl
    assert "--proto '=https' --proto-redir '=https'" in curl
    assert "-fsSL" in curl
    assert "--retry 5 --retry-delay 10" in curl
    assert "--location-trusted" not in curl


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checksum_check_follows_the_download_unchanged(path: Path) -> None:
    lines = [line.strip() for line in install_step(path)["run"].splitlines()]
    assert lines[curl_line(lines) + 1] == CHECKSUM_LINE
