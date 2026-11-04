"""RESTORE_WORK_DIR keeps the plaintext of a restore out of the default temp folder.

restore.sh writes the whole decrypted dump and backup_decrypt's work folder
under TMPDIR. vm-autostart.sh, and only it, reads the setting: every run sweeps
what a killed restore left in a private folder, a restore refuses a folder that
is not private before anything is created, and TMPDIR goes to restore.sh alone.

Slices are lifted by line from the real script and run under a fake colima,
docker and osascript and a stub restore.sh that records what it was handed:
step 6 alone, or a whole login from the lock to the end of the file. The last
test runs the real restore.sh.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import ProductSettings
from tests.shell import assignment, fake, lift, path_with
from tests.test_deploy_backup_restore import FAKE_DUMP, dumps, restore, run_backup

SETTING = "RESTORE_WORK_DIR"

# The script tells an ACL by the entry lines of macOS `ls -lde`, a flag GNU ls
# does not have, and CI runs on Linux. So every run gets a fake that answers
# that one call, as macOS does (a second line is an ACL entry), and hands the
# rest to the real ls. REAL_LS is for the macOS-only tests with a real ACL.
LS_PATH = shlex.quote(str(shutil.which("ls")))
NO_ACL_LS = (
    '[ "$1" = -lde ] && { echo "drwx------@ 2 fake fake 64 Nov 1 00:00 folder"; exit 0; }\n'
    f'exec {LS_PATH} "$@"\n'
)
ACL_LS = (
    '[ "$1" = -lde ] && { echo "drwx------@ 2 fake fake 64 Nov 1 00:00 folder"; '
    'echo " 0: group:everyone allow list,search"; exit 0; }\n'
    f'exec {LS_PATH} "$@"\n'
)
REAL_LS = f'exec {LS_PATH} "$@"\n'
DATE_PATH = shlex.quote(str(shutil.which("date")))
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="chmod +a and ls -e are macOS-only")
# The script reads a dump's name with BSD date, and CI runs on Linux. This fake
# answers those two forms as macOS does, measured there: a day its month lacks
# carries over (20260230 into March), so only the script's round trip rejects it,
# and what BSD strptime cannot parse fails. A changed form fails too; every call
# without -j goes to the real date.
FAKE_BSD_DATE = (
    f'[ "$1" = -j ] || exec {DATE_PATH} "$@"\n'
    f"exec {shlex.quote(sys.executable)} - \"$@\" <<'EOF'\n"
    """import calendar
import re
import sys
import time

FORMAT = "%Y%m%d-%H%M%S"
args = sys.argv[1:]
if args[:4] == ["-j", "-u", "-f", FORMAT] and args[5:] == ["+%s"]:
    match = re.fullmatch(r"(\\d{4})(\\d\\d)(\\d\\d)-(\\d\\d)(\\d\\d)(\\d\\d)", args[4])
    if match is None:
        sys.exit(1)
    year, month, day, hour, minute, second = (int(field) for field in match.groups())
    if not (1 <= month <= 12 and day <= 31 and hour <= 23 and minute <= 59 and second <= 60):
        sys.exit(1)
    print(calendar.timegm((year, month, day, hour, minute, second)))
elif args[:3] == ["-j", "-u", "-r"] and args[4:] == ["+" + FORMAT]:
    print(time.strftime(FORMAT, time.gmtime(int(args[3]))))
else:
    sys.exit(f"fake date: unknown form {args}")
EOF
"""
)


@pytest.fixture
def script(library_root: Path) -> Path:
    return library_root / "deploy" / "vm" / "vm-autostart.sh"


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    """A valid work folder; the space proves the script quotes it everywhere."""
    folder = tmp_path / "private work"
    folder.mkdir()
    folder.chmod(0o700)
    return folder


def use_bsd_date(bin_dir: Path) -> None:
    """Where date is not BSD, the fake goes in bin_dir; macOS keeps its real date."""
    if sys.platform != "darwin":
        fake(bin_dir, "date", FAKE_BSD_DATE)


def step_6(script: Path) -> str:
    """From entering the auto-restore through the restore.sh call."""
    lines = script.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith('echo "${PGDATA_VOLUME} is absent')]
    ends = [i for i, line in enumerate(lines) if 'deploy/restore.sh" "$newest"' in line]
    assert len(starts) == 1 and len(ends) == 1, f"step 6 moved; update this test: {starts} {ends}"
    assert starts[0] < ends[0], "the restore call moved ahead of step 6; update this test"
    # A ceiling, so a moved anchor cannot hand bash the rest of the script.
    assert ends[0] - starts[0] <= 130, "the slice ran past the restore call"
    return "\n".join(lines[starts[0] : ends[0] + 1]) + "\n"


def after_the_lock(script: Path) -> str:
    """A whole login: from taking the lock to the end of the script."""
    lines = script.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line == 'exec 9>"$LOCK"']
    assert len(starts) == 1, f"the lock moved; update this test: {starts}"
    return "\n".join(lines[starts[0] :]) + "\n"


@dataclass(frozen=True)
class Run:
    """What one run did: its result, its alerts, and what the fakes saw."""

    result: subprocess.CompletedProcess
    alerts: str
    docker: str
    # restore.sh's TMPDIR and the folder listing it saw; None and [] when it never ran.
    restore_tmpdir: str | None
    listing: list[str]


def build_world(
    tmp_path: Path,
    env: ProductSettings,
    volume_present: bool,
    extra_fakes: dict[str, str] | None,
    restore_body: str = "",
) -> None:
    """The fakes, the one dump, and the stub restore.sh, under tmp_path."""
    bin_dir = tmp_path / "bin"
    values = env.values
    volume = f"{values['COMPOSE_PROJECT']}_{values['DB_VOLUME']}" if volume_present else ""
    # Each call records the TMPDIR it can see: only restore.sh may get the folder.
    fake(
        bin_dir,
        "docker",
        f'printf "%s|%s\\n" "${{TMPDIR-UNSET}}" "$*" >>{shlex.quote(str(tmp_path / "docker.log"))}\n'
        f'[ "$1 $2" = "volume ls" ] && echo {shlex.quote(volume)}\n'
        f'[ "$1" = ps ] && echo {shlex.quote(values["DEPLOYER_SERVICE"])}\n'
        'case " $* " in *" psql "*) echo 3 ;; esac\n'
        "exit 0\n",
    )
    fake(bin_dir, "colima", f'[ "$1" = ssh ] && echo {shlex.quote(values["DATA_DISK_DEVICE"])}\nexit 0\n')
    fake(bin_dir, "osascript", f'printf "%s\\n" "$*" >>{shlex.quote(str(tmp_path / "alerts.log"))}\n')
    use_bsd_date(bin_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    fake(bin_dir, "ls", NO_ACL_LS)
    for name, body in (extra_fakes or {}).items():
        fake(bin_dir, name, body)

    backups = Path(values["BACKUP_DIR"])
    backups.mkdir(parents=True)
    dump = backups / f"{values['BACKUP_PREFIX']}-{stamp}.sql.gz.enc"
    dump.write_text("ciphertext")
    # The stub below verifies nothing; the selection only reads the v2 marker.
    Path(f"{dump}.hmac").write_text("v2 " + "0" * 64 + "\n")
    # The passphrase line reads the compose folder's secrets file; the stub
    # below needs no passphrase, so the file is left absent.
    checkout = Path(values["REPO_DIR"])
    (checkout / values["COMPOSE_DIR"]).mkdir(parents=True)
    stub = checkout / values["LIBRARY_DIR"] / "deploy" / "restore.sh"
    stub.parent.mkdir(parents=True)
    stub.write_text(
        f'printf %s "${{TMPDIR-UNSET}}" >{shlex.quote(str(tmp_path / "restore-tmpdir"))}\n'
        f'ls -A "$TMPDIR" >{shlex.quote(str(tmp_path / "restore-listing"))}\n' + restore_body
    )
    Path(values["APP_SUPPORT_DIR"]).mkdir(parents=True)
    vm_state = Path(values["COLIMA_HOME"]) / "_lima" / f"colima-{values['COLIMA_PROFILE']}" / "lima.yaml"
    vm_state.parent.mkdir(parents=True)
    vm_state.write_text("")


def run_code(tmp_path: Path, code: str, inherited_tmpdir: str | None) -> Run:
    run_env = path_with(tmp_path / "bin")
    run_env.pop("TMPDIR", None)
    if inherited_tmpdir is not None:
        run_env["TMPDIR"] = inherited_tmpdir
    result = subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=run_env, check=False)
    alerts = tmp_path / "alerts.log"
    docker_log = tmp_path / "docker.log"
    seen_tmpdir = tmp_path / "restore-tmpdir"
    seen_listing = tmp_path / "restore-listing"
    return Run(
        result=result,
        alerts=alerts.read_text() if alerts.exists() else "",
        docker=docker_log.read_text() if docker_log.exists() else "",
        restore_tmpdir=seen_tmpdir.read_text() if seen_tmpdir.exists() else None,
        listing=seen_listing.read_text().splitlines() if seen_listing.exists() else [],
    )


def prelude(script: Path, env: ProductSettings, names: tuple[str, ...]) -> str:
    """The settings, the script's own derived values, and its helper functions."""
    return (
        "set -eu\n"
        f". {shlex.quote(str(env.path))}\n"
        + "".join(assignment(script, name) + "\n" for name in names)
        + lift(script, "alert")
        + lift(script, "fail")
        + lift(script, "restore_work_dir_problem")
        + lift(script, "sweep_restore_work_dir")
    )


def run_step_6(
    tmp_path: Path,
    script: Path,
    env: ProductSettings,
    inherited_tmpdir: str | None,
    extra_fakes: dict[str, str] | None = None,
) -> Run:
    build_world(tmp_path, env, volume_present=False, extra_fakes=extra_fakes)
    names = (SETTING, "REPO", "BK", "MAX_DUMP_AGE_HOURS", "FAILED_MARK", "PGDATA_VOLUME")
    code = (
        prelude(script, env, names)
        + step_6(script)
        # What the cleanup trap reads, and whether the setting outlived the call.
        + 'RESTORE_OK=1\necho "TMPDIR after the call: ${TMPDIR-UNSET}"\n'
    )
    return run_code(tmp_path, code, inherited_tmpdir)


def run_login(
    tmp_path: Path,
    script: Path,
    env: ProductSettings,
    volume_present: bool,
    extra_fakes: dict[str, str] | None = None,
    restore_body: str = "",
) -> Run:
    build_world(tmp_path, env, volume_present, extra_fakes, restore_body)
    names = (
        SETTING,
        "DATA_DISK_DEVICE",
        "DEPLOYER_SERVICE",
        "OPS_PROFILE",
        "ERASURE_CHECK_SQL",
        "PROFILE",
        "REPO",
        "BK",
        "SOCKET",
        "VM_STATE",
        "LOCK",
        "FAILED_MARK",
        "MAX_DUMP_AGE_HOURS",
        "PGDATA_VOLUME",
    )
    return run_code(tmp_path, prelude(script, env, names) + after_the_lock(script), None)


def test_a_private_folder_reaches_restore_and_nothing_else(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    run = run_step_6(tmp_path, script, product_settings(**{SETTING: str(private_dir)}), str(inherited))
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert run.restore_tmpdir == str(private_dir)
    assert f"TMPDIR after the call: {inherited}" in run.result.stdout, "the setting leaked past the call"
    assert run.docker, "the fake docker never ran; the test proves nothing"
    assert str(private_dir) not in run.docker, f"docker saw the folder:\n{run.docker}"
    assert all(line.startswith(f"{inherited}|") for line in run.docker.splitlines()), run.docker


def test_the_login_restore_requires_v2_with_no_opt_in(
    tmp_path: Path, script: Path, product_settings: Callable[..., ProductSettings], private_dir: Path
) -> None:
    """The dump's marker makes the choice; this is what makes restore.sh enforce it."""
    seen = tmp_path / "restore-env"
    body = f'printf "%s|%s" "${{RESTORE_REQUIRE_V2-UNSET}}" "${{RESTORE_UNVERIFIED-UNSET}}" >{shlex.quote(str(seen))}\n'
    env = product_settings(**{SETTING: str(private_dir)})
    run = run_login(tmp_path, script, env, volume_present=False, restore_body=body)
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert seen.read_text() == "1|"


def plant_leftovers(folder: Path, env: ProductSettings) -> list[Path]:
    """What a killed restore leaves, by both patterns; returns those paths."""
    dump = folder / f"{env.values['PRODUCT']}-restore.abc123"
    dump.write_text("plaintext")
    err = folder / f"{env.values['PRODUCT']}-restore.abc123.err"
    err.write_text("stderr")
    crypto = folder / "backup-crypto.def456"
    crypto.mkdir()
    (crypto / "plaintext").write_text("plaintext")
    return [dump, err, crypto]


UNRELATED = ["otherproduct-restore.abc123", "unrelated.txt"]


def plant_unrelated(folder: Path) -> None:
    for name in UNRELATED:
        (folder / name).write_text("keep")


def assert_normal_path(run: Run) -> None:
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert "deployer is running" in run.result.stdout, run.result.stdout
    assert run.restore_tmpdir is None, "a restore ran with the volume present"


def test_every_login_sweeps_a_private_folder(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    """After a power loss the volume survives, so step 6 never runs again: the
    sweep cannot wait for a restore."""
    env = product_settings(**{SETTING: str(private_dir)})
    plant_leftovers(private_dir, env)
    plant_unrelated(private_dir)
    run = run_login(tmp_path, script, env, volume_present=True)
    assert_normal_path(run)
    assert sorted(p.name for p in private_dir.iterdir()) == UNRELATED
    assert run.alerts == "", run.alerts


def test_a_restore_starts_on_a_swept_folder(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    env = product_settings(**{SETTING: str(private_dir)})
    plant_leftovers(private_dir, env)
    plant_unrelated(private_dir)
    run = run_login(tmp_path, script, env, volume_present=False)
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert "restored: users=3" in run.result.stdout, run.result.stdout
    assert run.restore_tmpdir == str(private_dir)
    # What restore.sh saw when it started, not just what is there at the end.
    assert sorted(run.listing) == UNRELATED


def failing_restore(leftover: str) -> str:
    """A stub body: leaves a plaintext file in TMPDIR, as a killed child can, and fails."""
    return f'touch "$TMPDIR"/{shlex.quote(leftover)}\nexit 3\n'


def test_a_failed_restore_leaves_no_plaintext_behind(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    """The sweep must not change the exit status or the volume handling of the cleanup."""
    env = product_settings(**{SETTING: str(private_dir)})
    plant_unrelated(private_dir)
    leftover = f"{env.values['PRODUCT']}-restore.abc123"
    run = run_login(tmp_path, script, env, volume_present=False, restore_body=failing_restore(leftover))
    assert run.result.returncode == 3, run.result.stdout + run.result.stderr
    assert sorted(p.name for p in private_dir.iterdir()) == UNRELATED
    assert "auto-restore FAILED - removed its volume" in run.alerts, run.alerts


def test_a_failed_restore_with_a_stuck_leftover_keeps_its_exit_status(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    env = product_settings(**{SETTING: str(private_dir)})
    leftover = f"{env.values['PRODUCT']}-restore.abc123"
    run = run_login(
        tmp_path,
        script,
        env,
        volume_present=False,
        extra_fakes={"rm": "exit 1\n"},
        restore_body=failing_restore(leftover),
    )
    assert run.result.returncode == 3, run.result.stdout + run.result.stderr
    assert (private_dir / leftover).exists(), "the fake rm removed something; the test proves nothing"
    # One from the sweep after the lock, one from the cleanup.
    assert run.alerts.count("they may hold plaintext") == 2, run.alerts
    assert "auto-restore FAILED - removed its volume" in run.alerts, run.alerts


def test_an_empty_setting_makes_the_cleanup_sweep_nothing(
    tmp_path: Path, script: Path, product_settings: Callable[..., ProductSettings]
) -> None:
    env = product_settings(**{SETTING: ""})
    run = run_login(tmp_path, script, env, volume_present=False, restore_body="exit 3\n")
    assert run.result.returncode == 3, run.result.stdout + run.result.stderr
    assert "auto-restore FAILED - removed its volume" in run.alerts, run.alerts
    assert "plaintext" not in run.alerts, run.alerts


def test_a_missing_folder_at_login_says_nothing_and_goes_on(
    tmp_path: Path, script: Path, product_settings: Callable[..., ProductSettings]
) -> None:
    """An absent disk at a normal login must not alert or stop the VM."""
    env = product_settings(**{SETTING: str(tmp_path / "no-such-folder")})
    run = run_login(tmp_path, script, env, volume_present=True)
    assert_normal_path(run)
    assert run.alerts == "", run.alerts
    assert SETTING not in run.result.stdout + run.result.stderr


def test_a_stuck_leftover_alerts_and_the_login_goes_on(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    """A file that will not go must be said out loud, and must not stop the VM on every login."""
    env = product_settings(**{SETTING: str(private_dir)})
    leftovers = plant_leftovers(private_dir, env)
    run = run_login(tmp_path, script, env, volume_present=True, extra_fakes={"rm": "exit 1\n"})
    assert_normal_path(run)
    assert str(private_dir) in run.alerts and "plaintext" in run.alerts, run.alerts
    assert all(path.exists() for path in leftovers), "the fake rm removed something; the test proves nothing"


def missing(tmp_path: Path) -> str:
    return str(tmp_path / "no-such-folder")


def world_readable(tmp_path: Path) -> str:
    folder = tmp_path / "open"
    folder.mkdir()
    folder.chmod(0o755)
    return str(folder)


def group_readable(tmp_path: Path) -> str:
    folder = tmp_path / "group"
    folder.mkdir()
    folder.chmod(0o750)
    return str(folder)


def a_file(tmp_path: Path) -> str:
    path = tmp_path / "plain-file"
    path.write_text("")
    path.chmod(0o700)
    return str(path)


def symlink_to_private(tmp_path: Path) -> str:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    return str(link)


def symlink_with_a_trailing_slash(tmp_path: Path) -> str:
    """The form that resolves the link first, so -L alone would pass it."""
    return symlink_to_private(tmp_path) + "/"


def symlink_with_a_trailing_dot(tmp_path: Path) -> str:
    return symlink_to_private(tmp_path) + "/."


def relative(tmp_path: Path) -> str:
    return "relative/work"


def dangling_symlink(tmp_path: Path) -> str:
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "gone")
    return str(link)


def missing_with_a_trailing_slash(tmp_path: Path) -> str:
    return missing(tmp_path) + "/"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (missing, "not a directory"),
        (world_readable, "mode 700"),
        (group_readable, "mode 700"),
        (a_file, "not a directory"),
        (symlink_to_private, "symlink"),
        (dangling_symlink, "symlink"),
        (symlink_with_a_trailing_slash, "trailing"),
        (symlink_with_a_trailing_dot, "trailing"),
        (relative, "absolute"),
    ],
    ids=lambda v: v.__name__ if callable(v) else "",
)
def test_a_folder_that_is_not_private_refuses_before_anything_is_created(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    value: Callable[[Path], str],
    reason: str,
) -> None:
    run = run_step_6(tmp_path, script, product_settings(**{SETTING: value(tmp_path)}), None)
    assert run.result.returncode == 1, run.result.stdout + run.result.stderr
    assert SETTING in run.alerts and reason in run.alerts, run.alerts
    assert "refusing to restore" in run.alerts, run.alerts
    assert run.restore_tmpdir is None, "restore ran against a folder that is not private"
    # No docker call at all, so no compose up: the volume was not created.
    assert run.docker == "", f"docker ran:\n{run.docker}"
    assert "auto-restoring" not in run.alerts, run.alerts


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (symlink_to_private, "symlink"),
        (world_readable, "mode 700"),
        (group_readable, "mode 700"),
    ],
    ids=lambda v: v.__name__ if callable(v) else "",
)
def test_a_login_alerts_on_a_folder_that_is_not_private_and_removes_nothing(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    value: Callable[[Path], str],
    reason: str,
) -> None:
    folder = value(tmp_path)
    env = product_settings(**{SETTING: folder})
    leftovers = plant_leftovers(Path(folder).resolve(), env)
    run = run_login(tmp_path, script, env, volume_present=True)
    assert_normal_path(run)
    assert all(path.exists() for path in leftovers), "the sweep reached a folder that is not private"
    assert SETTING in run.alerts and reason in run.alerts, run.alerts


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (dangling_symlink, "symlink"),
        (symlink_with_a_trailing_slash, "trailing"),
        (missing_with_a_trailing_slash, "trailing"),
        (a_file, "not a directory"),
        (relative, "absolute"),
    ],
    ids=lambda v: v.__name__ if callable(v) else "",
)
def test_a_login_alerts_on_a_bad_path_and_goes_on(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    value: Callable[[Path], str],
    reason: str,
) -> None:
    """Only an absolute path with nothing at all there can be an absent disk."""
    run = run_login(tmp_path, script, product_settings(**{SETTING: value(tmp_path)}), volume_present=True)
    assert_normal_path(run)
    assert SETTING in run.alerts and reason in run.alerts, run.alerts


def test_an_acl_refuses_at_step_6(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    env = product_settings(**{SETTING: str(private_dir)})
    run = run_step_6(tmp_path, script, env, None, extra_fakes={"ls": ACL_LS})
    assert run.result.returncode == 1, run.result.stdout + run.result.stderr
    assert SETTING in run.alerts and "ACL" in run.alerts, run.alerts
    assert "refusing to restore" in run.alerts, run.alerts
    assert run.restore_tmpdir is None, "restore ran against a folder with an ACL"
    assert run.docker == "", f"docker ran:\n{run.docker}"


def test_an_acl_alerts_at_login_and_removes_nothing(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    env = product_settings(**{SETTING: str(private_dir)})
    leftovers = plant_leftovers(private_dir, env)
    run = run_login(tmp_path, script, env, volume_present=True, extra_fakes={"ls": ACL_LS})
    assert_normal_path(run)
    assert SETTING in run.alerts and "ACL" in run.alerts, run.alerts
    assert all(path.exists() for path in leftovers), "the sweep reached a folder with an ACL"


def test_a_folder_gone_bad_during_a_failed_restore_keeps_the_cleanup_whole(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    """The alert from the sweep must not change the cleanup's exit status or volume handling."""
    env = product_settings(**{SETTING: str(private_dir)})
    leftover = f"{env.values['PRODUCT']}-restore.abc123"
    body = 'chmod 755 "$TMPDIR"\n' + failing_restore(leftover)
    run = run_login(tmp_path, script, env, volume_present=False, restore_body=body)
    assert run.result.returncode == 3, run.result.stdout + run.result.stderr
    assert (private_dir / leftover).exists(), "the sweep reached a folder that is not private"
    assert "mode 700" in run.alerts, run.alerts
    assert "auto-restore FAILED - removed its volume" in run.alerts, run.alerts


def real_acl(folder: Path) -> None:
    """The entry a review measured: without the check, files inside inherit read for everyone."""
    entry = "everyone allow list,search,read,file_inherit,directory_inherit"
    subprocess.run(["chmod", "+a", entry, str(folder)], check=True)


def real_xattr(folder: Path) -> None:
    subprocess.run(["xattr", "-w", "user.test", "value", str(folder)], check=True)


@macos_only
@pytest.mark.parametrize(
    ("acl", "xattr", "refused"),
    [(False, False, False), (True, False, True), (False, True, False), (True, True, True)],
    ids=["plain", "acl", "xattr", "both"],
)
def test_the_real_ls_tells_an_acl_from_an_xattr(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
    acl: bool,
    xattr: bool,
    refused: bool,
) -> None:
    """Measured on macOS: `ls -ld` shows @ for an xattr and hides a + behind it, so it cannot tell."""
    if acl:
        real_acl(private_dir)
    if xattr:
        real_xattr(private_dir)
    env = product_settings(**{SETTING: str(private_dir)})
    run = run_step_6(tmp_path, script, env, None, extra_fakes={"ls": REAL_LS})
    if refused:
        assert run.result.returncode == 1, run.result.stdout + run.result.stderr
        assert "ACL" in run.alerts, run.alerts
        assert run.restore_tmpdir is None, "restore ran against a folder with an ACL"
    else:
        assert run.result.returncode == 0, run.result.stdout + run.result.stderr
        assert run.restore_tmpdir == str(private_dir)


@macos_only
def test_a_real_acl_alerts_at_login_and_removes_nothing(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    private_dir: Path,
) -> None:
    env = product_settings(**{SETTING: str(private_dir)})
    leftovers = plant_leftovers(private_dir, env)
    real_acl(private_dir)
    run = run_login(tmp_path, script, env, volume_present=True, extra_fakes={"ls": REAL_LS})
    assert_normal_path(run)
    assert "ACL" in run.alerts, run.alerts
    assert all(path.exists() for path in leftovers), "the sweep reached a folder with an ACL"


def test_an_empty_setting_leaves_the_inherited_temp_folder(
    tmp_path: Path, script: Path, product_settings: Callable[..., ProductSettings]
) -> None:
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    env = product_settings(**{SETTING: ""})
    # Not autostart's folder to clean: a shared temp folder is never swept.
    other_run = inherited / f"{env.values['PRODUCT']}-restore.abc123"
    other_run.write_text("someone else's")
    run = run_step_6(tmp_path, script, env, str(inherited))
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert run.restore_tmpdir == str(inherited)
    assert other_run.exists()


def test_a_product_without_the_setting_behaves_as_empty(
    tmp_path: Path, script: Path, product_settings: Callable[..., ProductSettings]
) -> None:
    """An installed product.settings written before the setting existed, under set -u."""
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    env = product_settings()
    kept = [line for line in env.path.read_text().splitlines() if not line.startswith(f"{SETTING}=")]
    env.path.write_text("\n".join(kept) + "\n")
    run = run_step_6(tmp_path, script, env, str(inherited))
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert run.restore_tmpdir == str(inherited)


@pytest.mark.parametrize(
    ("inherited", "after"),
    [(None, "UNSET"), ("", "")],
    ids=["unset", "empty"],
)
def test_no_setting_and_no_tmpdir_means_the_tmp_default(
    tmp_path: Path,
    script: Path,
    product_settings: Callable[..., ProductSettings],
    inherited: str | None,
    after: str,
) -> None:
    """restore.sh's own ${TMPDIR:-/tmp} treats unset and empty alike, so this is today's folder."""
    run = run_step_6(tmp_path, script, product_settings(**{SETTING: ""}), inherited)
    assert run.result.returncode == 0, run.result.stdout + run.result.stderr
    assert run.restore_tmpdir == "/tmp"
    assert f"TMPDIR after the call: {after}\n" in run.result.stdout, "the default leaked past the call"


def test_the_real_restore_puts_both_plaintext_files_under_tmpdir(
    tmp_path: Path, product_settings: Callable[..., ProductSettings], private_dir: Path
) -> None:
    """Every mktemp restore.sh and backup_decrypt make, and the folder while the
    dump is being decrypted. backup_decrypt's folder is gone by the time psql
    runs, so a fake psql could not see it."""
    env = product_settings(ERASURE_CHECK_SQL="")
    backups = run_backup(tmp_path, env)[0]
    wrappers = tmp_path / "wrappers"
    templates = tmp_path / "mktemp-args"
    during = tmp_path / "during-decrypt"
    fake(
        wrappers,
        "mktemp",
        f'printf "%s\\n" "${{@: -1}}" >>{shlex.quote(str(templates))}\n'
        f'exec {shlex.quote(str(shutil.which("mktemp")))} "$@"\n',
    )
    fake(
        wrappers,
        "openssl",
        f'case " $* " in *" -d "*) find "$TMPDIR" -mindepth 1 >>{shlex.quote(str(during))} ;; esac\n'
        f'exec {shlex.quote(str(shutil.which("openssl")))} "$@"\n',
    )
    result, received = restore(
        tmp_path, env, dumps(backups, env)[0], path_prefix=str(wrappers), TMPDIR=str(private_dir)
    )
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP

    product = env.values["PRODUCT"]
    made = templates.read_text().splitlines()
    assert any(t.startswith(f"{private_dir}/{product}-restore.") for t in made), made
    assert any(t.startswith(f"{private_dir}/backup-crypto.") for t in made), made
    assert all(t.startswith(f"{private_dir}/") for t in made), f"a temp file went elsewhere: {made}"

    seen = during.read_text().splitlines()
    assert any(p.startswith(f"{private_dir}/{product}-restore.") for p in seen), seen
    assert any(p.startswith(f"{private_dir}/backup-crypto.") for p in seen), seen

    assert os.listdir(private_dir) == [], "the restore left plaintext behind"
