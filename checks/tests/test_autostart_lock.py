"""The login agent's lock must not outlive the run that took it.

vm-autostart.sh holds a flock on descriptor 9 for its whole run, so two
starters never race. colima start leaves daemons behind, and a child inherits
every open descriptor: a VM whose daemons outlived it held the lock, so every
later run said "another autostart run holds the lock" and left. A limactl that
hung, or a usernet that daemonized, did the same. So every colima, docker and
osascript call, and restore.sh, closes descriptor 9 and runs under
`timeout -k 10 N`.

The start line is lifted out of the real script and run with the lock open
and a fake colima that reports whether it can see descriptor 9. The rule for
every other call is checked statically over the script text.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.shell import fake, path_with

# A fail() that says what it was handed; the real one also alerts via osascript.
FAIL = 'fail() { echo "fail: $*"; exit 1; }'


@pytest.fixture
def script(library_root: Path) -> Path:
    return library_root / "deploy" / "vm" / "vm-autostart.sh"


def start_line(script: Path) -> str:
    found = [
        line.strip()
        for line in script.read_text(encoding="utf-8").splitlines()
        if "colima start " in line and not line.strip().startswith("#")
    ]
    assert len(found) == 1, f"expected one colima start line, found {found}"
    return found[0]


def seen_by_colima(tmp_path: Path, line: str) -> str:
    bin_dir = tmp_path / "bin"
    fake(bin_dir, "colima", "if { true >&9; } 2>/dev/null; then echo inherited; else echo closed; fi\n")
    done = subprocess.run(
        ["bash", "-c", f'set -eu\n{FAIL}\nexec 9>"$LOCK"\nPROFILE=fakevm\n{line}\n'],
        env={**path_with(bin_dir), "LOCK": str(tmp_path / "lock")},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_colima_does_not_inherit_the_lock(tmp_path: Path, script: Path) -> None:
    assert seen_by_colima(tmp_path, start_line(script)) == "closed"


def test_the_harness_can_tell_an_inherited_lock(tmp_path: Path) -> None:
    """The positive control: the line as it was before the fix."""
    assert seen_by_colima(tmp_path, 'colima start --profile "$PROFILE"') == "inherited"


@pytest.mark.parametrize(
    ("body", "exit_code", "bound"),
    [
        # The fake sleeps 30 s, far past both bounds, so a run that waited for
        # it cannot pass on a slow runner.
        ("sleep 30", "exit 124", 4.5),
        # `trap '' TERM` ignores SIGTERM, and sleep inherits the ignored
        # disposition, so only the -k SIGKILL a second after the limit ends
        # the pair: timeout then exits 137, which the script's checks treat as
        # a timeout alongside 124. `exec` makes sleep the process timeout
        # started, so the KILL reaches it even where timeout signals only its
        # child, not the group (uutils timeout on Ubuntu 26.04, uutils#15768).
        ("trap '' TERM\nexec sleep 30", "exit 137", 5.5),
    ],
    ids=["honours-TERM", "ignores-TERM"],
)
def test_a_hung_colima_start_fails_instead_of_waiting(
    tmp_path: Path, script: Path, body: str, exit_code: str, bound: float
) -> None:
    """The real start line, with only its limit and grace shortened, against a
    colima that never returns: the run must end, through fail(), and say why."""
    line = start_line(script)
    assert line.startswith("timeout -k 10 300 "), f"the start line lost its limit: {line}"
    bin_dir = tmp_path / "bin"
    fake(bin_dir, "colima", f"{body}\n")
    began = time.monotonic()
    done = subprocess.run(
        [
            "bash",
            "-c",
            f"set -eu\n{FAIL}\nPROFILE=fakevm\ntimeout -k 1 1 {line.removeprefix('timeout -k 10 300 ')}\n",
        ],
        env=path_with(bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert time.monotonic() - began < bound, "the run waited for the hung colima"
    assert done.returncode != 0
    assert "300s" in done.stdout and exit_code in done.stdout, done.stdout


# Command position: line start, after a separator or `$(`, or after
# `timeout -k 10 N`. restore.sh is called by its path in the library subtree.
CALL = re.compile(r'(?:^|[\s;&|]|\$\()(colima|docker|osascript|bash (?:"[^"]*/)?restore\.sh"?)(?=\s)')
# `timeout -k 10 N`, not `timeout N`: plain timeout only sends SIGTERM and
# waits, and a docker CLI forwards that to the container and waits too, so the
# limit was no bound at all. -k 10 sends SIGKILL 10 s later.
GUARD = re.compile(r"timeout -k 10 \d+\s+$")
NO_GRACE = re.compile(r"timeout \d+\s+$")


def keep(quoted: str) -> bool:
    """A double-quoted string whose text runs: a $( ) inside it, or restore.sh's own path."""
    return "$(" in quoted or quoted.endswith('restore.sh"')


def logical_lines(text: str) -> list[str]:
    """Comment lines dropped, backslash continuations joined, and the text of
    quoted strings blanked - except a double-quoted string that holds a $( ),
    whose command runs, or names restore.sh, which is the call. That keeps
    "refusing to run colima" in a message from counting as a call, while
    `$(docker ps ...)` inside an echo still counts.
    """
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        if raw.endswith("\\"):
            pending += raw[:-1] + " "
            continue
        joined.append(pending + raw)
        pending = ""
    out = []
    for line in joined:
        line = re.sub(r'"[^"]*"', lambda m: m.group(0) if keep(m.group(0)) else '""', line)
        out.append(re.sub(r"'[^']*'", "''", line))
    return out


def calls(text: str) -> list[tuple[str, str]]:
    """(line, the text before the call) for every call to colima, docker,
    osascript or restore.sh in the script."""
    return [(line, line[: m.start(1)]) for line in logical_lines(text) for m in CALL.finditer(line)]


def assert_guarded(text: str) -> None:
    unguarded = []
    for line, before in calls(text):
        if GUARD.search(before):
            continue
        if NO_GRACE.search(before):
            unguarded.append(f"no -k grace: {line.strip()}")
        else:
            unguarded.append(f"no timeout: {line.strip()}")
    for line in {line for line, _ in calls(text)}:
        if line.count("9>&-") < len(CALL.findall(line)):
            unguarded.append(f"lock not closed: {line.strip()}")
    assert not unguarded, "\n".join(unguarded)


def test_every_child_closes_the_lock_and_has_a_limit(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    found = calls(text)
    # 20 today. A floor, so a rewrite that hides calls from the pattern cannot
    # pass by finding none.
    assert len(found) >= 18, f"found only {len(found)} calls: {found}"
    assert any("restore.sh" in line for line, _ in found), "the restore.sh call is not seen by the check"
    assert_guarded(text)


@pytest.mark.parametrize(
    "line",
    [
        'if colima status "$PROFILE" >/dev/null 2>&1; then',
        'colima start --profile "$PROFILE" 9>&-',
        'DOCKER_DEV=$(colima ssh --profile "$PROFILE" -- findmnt 2>/dev/null || echo none)',
        "    timeout 10 docker version >/dev/null 2>&1 && break",
        'RESTORE_PROJECT=stack \\\n    bash restore.sh "$newest"',
        'timeout -k 10 1800 bash "${REPO}/${LIBRARY_DIR}/deploy/restore.sh" "$newest"',
    ],
)
def test_the_check_rejects_an_unguarded_call(line: str) -> None:
    """The positive control: the shapes the script had before the fix."""
    with pytest.raises(AssertionError):
        assert_guarded(line + "\n")


def test_the_check_rejects_a_timeout_without_kill_grace() -> None:
    """The positive control for -k: the form the script had before it."""
    with pytest.raises(AssertionError, match="no -k grace"):
        assert_guarded("timeout 10 docker version >/dev/null 2>&1 && break\n")


def test_a_message_naming_a_tool_is_not_a_call() -> None:
    assert calls('fail "refusing to run colima (it would) - data-disk check (colima ssh findmnt)"\n') == []


def step_2(tmp_path: Path, script: Path, status_exit: int) -> subprocess.CompletedProcess:
    """Step 2, lifted by line from the script, under a colima whose status exits as told.

    From the `rc=0` before `colima status` through the closing `fi`, bounded
    by length so an indented `fi` cannot extend the slice into step 3. The
    lifted text is run, not added to the script, so start_line() still finds
    one line.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    anchors = [
        i for i, line in enumerate(lines) if line.startswith('timeout -k 10 10 colima status "$PROFILE"')
    ]
    assert len(anchors) == 1, f"expected one colima status line, found lines {anchors}"
    start = anchors[0] - 1
    end = next((i for i in range(start, len(lines)) if lines[i] == "fi"), None)
    assert end is not None, "step 2 has no closing fi after the colima status line; update this test"
    lifted = "\n".join(lines[start : end + 1]) + "\n"
    assert lifted.startswith("rc=0\ntimeout -k 10 10 colima status"), (
        f"step 2 moved; update this test:\n{lifted}"
    )
    assert lifted.count("\n") <= 20, f"the slice ran past step 2:\n{lifted}"
    bin_dir = tmp_path / "bin"
    fake(
        bin_dir,
        "colima",
        f'[ "$1" = status ] && exit {status_exit}\n[ "$1" = start ] && {{ echo "fake start"; exit 0; }}\nexit 99\n',
    )
    return subprocess.run(
        ["bash", "-c", f"set -eu\n{FAIL}\nPROFILE=fakevm\n{lifted}"],
        env=path_with(bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_running_profile_is_left_alone(tmp_path: Path, script: Path) -> None:
    done = step_2(tmp_path, script, status_exit=0)
    assert done.returncode == 0, done.stdout
    assert "already running" in done.stdout and "fake start" not in done.stdout


def test_a_status_that_did_not_answer_says_so_and_starts(tmp_path: Path, script: Path) -> None:
    """A fake colima exiting 124 stands in for timeout's own 124 (limit hit):
    GNU timeout passes its child's status through unchanged. The log must not
    read as if the profile were simply not running."""
    done = step_2(tmp_path, script, status_exit=124)
    assert done.returncode == 0, done.stdout
    for text in ("did not answer", "exit 124", "starting colima", "fake start"):
        assert text in done.stdout, done.stdout


def test_a_stopped_profile_starts_without_a_timeout_message(tmp_path: Path, script: Path) -> None:
    done = step_2(tmp_path, script, status_exit=1)
    assert done.returncode == 0, done.stdout
    assert "starting colima" in done.stdout and "fake start" in done.stdout
    assert "did not answer" not in done.stdout
