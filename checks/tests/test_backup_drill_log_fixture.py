"""The operator drill's log fixture must not depend on the age of the build cache."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from conftest import LIBRARY_ROOT
from tests.shell import fake


def test_log_fixture_differs_between_runs(tmp_path: Path) -> None:
    """A fixture identical to an earlier run's reuses its cached image layer.

    The layer keeps the earlier run's file times, and logarchive.sh skips a log
    not written to in the last day, so the drill would fail by the age of the
    build cache. Stopping at the build is enough: the build context is the input.
    """
    seen = tmp_path / "fixture-logs"
    fake(
        tmp_path / "bin",
        "docker",
        """
case "$1" in
    info) printf 'fake-drill-daemon\n' ;;
    image|network|volume|container) [ "$2" != inspect ] || exit 1 ;;
    build)
        cat "${@: -1}/logs/fixture/fixture-json.log" >>"$DRILL_SEEN"
        exit 1 ;;
esac
""",
    )
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    for _ in range(2):
        result = subprocess.run(
            [
                "bash",
                str(LIBRARY_ROOT / "checks" / "backup-restore-drill.sh"),
                str(tmp_path / "report"),
                str(parent),
            ],
            env={
                **os.environ,
                "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
                "DOCKER_HOST": "unix:///fixture/docker.sock",
                "DRILL_SEEN": str(seen),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert result.returncode != 0, "the fixture refuses the build"
    lines = seen.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] != lines[1]
