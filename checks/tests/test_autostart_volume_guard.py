"""The login agent must not read a failed docker call as "the database is gone".

Step 5 of vm-autostart.sh decides whether the database volume exists.
Answered wrong in the safe direction it costs a restart. Answered wrong in the
other it runs step 6 - an unattended restore - against a live database, which
aborts on the first duplicate key and fires a cleanup trap that removes the
volume. So the question is not "did grep match" but "did docker answer".

The functions are lifted out of the real script and run under bash with a
fake docker, after the fake product.settings the installed script would source.
The script's flags are reproduced in the prelude: without them a test
depending on errexit would quietly prove something else.
"""

from __future__ import annotations

import calendar
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import ProductSettings
from tests.shell import assignment, fake, lift, path_with
from tests.test_autostart_restore_work_dir import step_6, use_bsd_date

macos_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="needs the real BSD date's time-zone handling"
)
V2_SIDECAR = "v2 " + "0" * 64 + "\n"
LEGACY_SIDECAR = "0" * 64 + "\n"


@pytest.fixture
def script(library_root: Path) -> Path:
    return library_root / "deploy" / "vm" / "vm-autostart.sh"


@pytest.fixture
def env(product_settings: Callable[..., ProductSettings]) -> ProductSettings:
    return product_settings()


@pytest.fixture(autouse=True)
def bsd_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dump choice runs under whatever date is first on PATH: see use_bsd_date."""
    bin_dir = tmp_path / "date-bin"
    use_bsd_date(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def prelude(script: Path, env: ProductSettings) -> str:
    """The script's settings and its volume name, derived by its own line; alert() stubbed."""
    return (
        "set -eu\n"
        f". {shlex.quote(str(env.path))}\n"
        f"{assignment(script, 'PGDATA_VOLUME')}\n"
        'alert() { echo "[alert] $*" >&2; }\n'
    )


@pytest.fixture
def volume(script: Path, env: ProductSettings) -> str:
    """The database volume name exactly as the script derives it."""
    return subprocess.check_output(
        ["bash", "-c", prelude(script, env) + 'printf %s "$PGDATA_VOLUME"'], text=True
    )


def answering_docker(bin_dir: Path, answer: str, exit_code: int) -> None:
    # Real docker writes its errors to stderr, so the fake must too - otherwise
    # dropping a 2>&1 from a capture would look harmless here while the alert
    # lost its reason in production.
    fake(
        bin_dir,
        "docker",
        f"printf '%s\\n' {shlex.quote(answer)}{' >&2' if exit_code else ''}\nexit {exit_code}\n",
    )


def run(
    tmp_path: Path, script: Path, env: ProductSettings, volumes: str = "", exit_code: int = 0
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    answering_docker(bin_dir, volumes, exit_code)
    # The real fail(), lifted rather than restated. Errexit is suppressed
    # inside a function used as an `if` condition, so the guard rests entirely
    # on fail() calling exit - a hand-written stand-in would pin the caller's
    # behaviour while leaving that premise untested.
    code = (
        prelude(script, env)
        + lift(script, "fail")
        + lift(script, "pgdata_present")
        + "if pgdata_present; then echo PRESENT; else echo ABSENT; fi"
    )
    return subprocess.run(
        ["bash", "-c", code], capture_output=True, text=True, env=path_with(bin_dir), check=False
    )


def test_the_volume_is_found_when_it_is_there(
    tmp_path: Path, script: Path, env: ProductSettings, volume: str
) -> None:
    result = run(tmp_path, script, env, volumes=f"{volume}\nsomething_else")
    assert result.returncode == 0, result.stderr
    assert "PRESENT" in result.stdout


def test_a_genuinely_absent_volume_is_reported_absent(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    """The restore path exists for exactly this: a real machine with real
    docker and no database volume."""
    result = run(tmp_path, script, env, volumes="other_volume")
    assert result.returncode == 0, result.stderr
    assert "ABSENT" in result.stdout


def test_a_failed_read_refuses_instead_of_answering_absent(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    """The whole point. A docker that cannot answer used to produce no output,
    grep exited 1, and the condition was false - identical to a machine with
    no volume, which sends the script into an unattended restore whose cleanup
    deletes the live volume.
    """
    result = run(tmp_path, script, env, volumes="Cannot connect to the Docker daemon", exit_code=1)
    assert result.returncode == 1, "a failed docker read was treated as an answer"
    assert "ABSENT" not in result.stdout, "a failed read fell through to the restore path"
    assert "refusing to decide" in result.stderr


def test_the_refusal_names_the_reason(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    """The capture takes 2>&1 so docker's error lands in the alert.

    Asserting only that the text appears in stderr proves nothing: drop the
    redirect and docker's stderr passes straight through to the process, so a
    bare `in result.stderr` passes on exactly the mutant it exists to catch.
    The reason has to be inside the composed message.
    """
    result = run(tmp_path, script, env, volumes="permission denied while trying to connect", exit_code=1)
    assert "docker volumes (permission denied" in result.stderr


def test_a_similarly_named_volume_does_not_answer_for_it(
    tmp_path: Path, script: Path, env: ProductSettings, volume: str
) -> None:
    """grep -qx, not -q: <volume>_old is a different volume, and matching it
    would report the database present when it is not."""
    result = run(tmp_path, script, env, volumes=f"{volume}_old\nx{volume}")
    assert "ABSENT" in result.stdout


def test_the_guard_is_wired_into_step_5(script: Path) -> None:
    """The function is driven standalone here, so nothing else would notice if
    the call site went back to the original pipe and left this as dead code."""
    assert "if pgdata_present; then" in script.read_text(encoding="utf-8")


def clause(
    tmp_path: Path, script: Path, env: ProductSettings, answer: str = "", exit_code: int = 0
) -> subprocess.CompletedProcess:
    """erasure_clause, lifted from the script, under a docker that answers as told.

    The fake writes its text to stdout on success and to stderr on failure,
    because the script keeps only stdout and the difference is the whole
    point: a psql error must not become the timestamp.
    """
    bin_dir = tmp_path / "bin"
    answering_docker(bin_dir, answer, exit_code)
    # Bracketed, because the alert interpolates the clause INTO a sentence and
    # because subprocess's text mode translates a stray \r into \n - at the end
    # of a line that hides it completely, mid-line it splits the line and the
    # test can see it.
    code = prelude(script, env) + lift(script, "erasure_clause") + "printf '[%s]\\n' \"$(erasure_clause)\""
    return subprocess.run(
        ["bash", "-c", code], capture_output=True, text=True, env=path_with(bin_dir), check=False
    )


def test_a_timestamp_names_the_boundary(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    result = clause(tmp_path, script, env, answer="2026-09-11 04:00:00+00")
    assert "any data-deletion request after 2026-09-11 04:00:00+00" in result.stdout


def test_a_carriage_return_does_not_reach_the_alert(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    """psql through `compose exec -T` can return CRLF. The stripping used to
    be a `tr` in a pipeline, and removing the pipeline is what fixed the exit
    status - so the replacement has to actually strip."""
    result = clause(tmp_path, script, env, answer="2026-09-11 04:00:00+00\r")
    assert "04:00:00+00]" in result.stdout, (
        "the timestamp still carries a carriage return, which puts the rest of "
        f"the alert on top of it in a notification:\n{result.stdout!r}"
    )


def test_an_empty_table_is_the_widest_case(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    """No row survives, so nothing bounds what the restore undid."""
    result = clause(tmp_path, script, env, answer="")
    assert "EVERY data-deletion request this deployment ever received" in result.stdout


def test_a_failed_read_is_not_reported_as_an_empty_table(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    """Postgres is routinely not up yet on this path - the restore has just
    finished and the stack is still coming back - and reading that as "no
    erasure request survives" is the loudest message the script can send.
    Cry wolf here and the true one gets ignored.
    """
    result = clause(tmp_path, script, env, answer="could not connect to server", exit_code=1)
    assert "EVERY" not in result.stdout, "a failed read was reported as an empty table"
    assert "could not be read" in result.stdout


def test_the_clause_is_wired_into_the_completion_alert_and_skipped_without_a_query(script: Path) -> None:
    """Driven standalone above, so nothing else would notice if the alert went
    back to interpolating the raw query. A product with no erasure record sets
    ERASURE_CHECK_SQL empty, and then there is no alert to raise."""
    body = script.read_text(encoding="utf-8")
    assert (
        '[ -z "$ERASURE_CHECK_SQL" ] || alert "ERASURES UNDONE by auto-restore: $(erasure_clause) is gone'
        in body
    )


def cleanup(
    tmp_path: Path, script: Path, env: ProductSettings, volumes: str, restore_ok: str = "0"
) -> tuple[subprocess.CompletedProcess, Path]:
    """restore_cleanup, lifted from the script, under a docker whose `volume ls` answers as told."""
    bin_dir = tmp_path / "bin"
    fake(bin_dir, "docker", f'[ "$1 $2" = "volume ls" ] && printf "%s" {shlex.quote(volumes)}\nexit 0\n')
    mark = tmp_path / "restore-failed"
    assert not mark.exists()
    code = (
        prelude(script, env)
        + f"RESTORE_OK={restore_ok}\nFAILED_MARK={shlex.quote(str(mark))}\n"
        + lift(script, "restore_cleanup")
        + "restore_cleanup"
    )
    result = subprocess.run(
        ["bash", "-c", code], capture_output=True, text=True, env=path_with(bin_dir), check=False
    )
    return result, mark


def test_a_volume_left_behind_leaves_a_marker(
    tmp_path: Path, script: Path, env: ProductSettings, volume: str
) -> None:
    """The next login finds the volume present; only the marker stops it starting the deployer on it."""
    result, mark = cleanup(tmp_path, script, env, volumes=f"{volume}\n")
    assert mark.exists(), "a failed restore left its volume behind and no marker"
    assert "was NOT removed" in result.stderr


def test_a_removed_volume_leaves_no_marker(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    """The volume is gone, so the next login retries the restore; a marker would block that."""
    result, mark = cleanup(tmp_path, script, env, volumes="other_volume\n")
    assert not mark.exists()
    assert "removed its volume so the next login retries" in result.stderr


def test_a_successful_restore_cleans_up_nothing(
    tmp_path: Path, script: Path, env: ProductSettings, volume: str
) -> None:
    result, mark = cleanup(tmp_path, script, env, volumes=f"{volume}\n", restore_ok="1")
    assert result.returncode == 0
    assert not mark.exists()
    assert "[alert]" not in result.stderr


def test_the_marker_is_wired_into_step_5_and_the_restore(script: Path) -> None:
    """Driven standalone above, so nothing else would notice if step 5 stopped checking the
    marker, checked it only after waiting for the deployer, or a good restore stopped clearing it."""
    body = script.read_text(encoding="utf-8")
    step_5 = body.split("if pgdata_present; then", 1)[1].split("\n    exit 0\n", 1)[0]
    assert '[ ! -e "$FAILED_MARK" ]' in step_5
    assert step_5.index('[ ! -e "$FAILED_MARK" ]') < step_5.index("for _ in $(seq 1 12)")
    # The removal follows the flag (the data is kept before the marker goes) and
    # a removal that fails alerts instead of ending the run under set -e.
    after_flag = body.split("\nRESTORE_OK=1\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(r'^rm -f "\$FAILED_MARK" \|\| alert ', after_flag, re.MULTILINE), after_flag


def users_count(
    tmp_path: Path, script: Path, env: ProductSettings, answer: str = "", exit_code: int = 0
) -> subprocess.CompletedProcess:
    """The count after the restore, lifted from the script, under a docker that answers as told.

    The block is not a function, so it is lifted by line: from the `rc=0`
    before the count through the `restored:` echo, bounded by length so a
    moved end anchor cannot hand bash half the script. A fake docker exiting
    124 or 137 stands in for timeout's own 124 (limit hit) and 137 (killed):
    GNU timeout passes its child's status through unchanged. The fake records
    its argv, so the test can see which query reached psql.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    anchors = [i for i, line in enumerate(lines) if line.startswith("USERS=$(timeout")]
    assert len(anchors) == 1, f"expected one users count, found lines {anchors}"
    start = anchors[0] - 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith('echo "restored:'))
    lifted = "\n".join(lines[start : end + 1]) + "\n"
    assert lifted.startswith("rc=0\nUSERS=$(timeout"), f"the users count moved; update this test:\n{lifted}"
    # A ceiling, not an exact count: the block may grow a check, but a slice
    # past ~20 lines has run into the next step.
    assert lifted.count("\n") <= 20, f"the slice ran past the users count:\n{lifted}"
    assert lifted.count("USERS=$(") == 1 and "RESTORE_OK=1" not in lifted, lifted
    bin_dir = tmp_path / "bin"
    argv = tmp_path / "docker-argv"
    fake(
        bin_dir,
        "docker",
        f'printf "%s\\n" "$*" >>{shlex.quote(str(argv))}\nprintf \'%s\\n\' {shlex.quote(answer)}\nexit {exit_code}\n',
    )
    code = prelude(script, env) + lift(script, "fail") + lifted
    return subprocess.run(
        ["bash", "-c", code], capture_output=True, text=True, env=path_with(bin_dir), check=False
    )


def test_a_restored_database_reports_its_users(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    result = users_count(tmp_path, script, env, answer="3")
    assert result.returncode == 0, result.stderr
    assert "restored: users=3" in result.stdout


def test_the_count_is_the_products_own_query(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    """RESTORE_VERIFY_SQL from product.settings is what reaches psql, against the product's database."""
    users_count(tmp_path, script, env, answer="3")
    argv = (tmp_path / "docker-argv").read_text()
    assert env.values["RESTORE_VERIFY_SQL"] in argv, argv
    for key in ("COMPOSE_PROJECT", "DB_SERVICE", "DB_USER", "DB_NAME"):
        assert env.values[key] in argv, f"{key} did not reach the count: {argv}"


def test_an_empty_users_table_fails(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    result = users_count(tmp_path, script, env, answer="0")
    assert result.returncode == 1
    assert "users table is empty" in result.stderr


def test_an_empty_answer_reads_as_an_empty_table(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    result = users_count(tmp_path, script, env, answer="")
    assert result.returncode == 1
    assert "users table is empty" in result.stderr and "integer" not in result.stderr


def test_an_answer_that_is_not_an_integer_fails_by_name(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    """A query that returns text (a misconfigured RESTORE_VERIFY_SQL) must say
    so, not die inside `[ -ge ]` with a shell error that reaches no alert."""
    result = users_count(tmp_path, script, env, answer="relation missing")
    assert result.returncode == 1
    assert "RESTORE_VERIFY_SQL did not return an integer: relation missing" in result.stderr
    assert "integer expression expected" not in result.stderr


def test_a_count_that_timed_out_is_not_an_empty_table(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    """Without `|| rc=$?` errexit ends the run at the assignment, exit 124 and
    no alert at all - and the cleanup then removes the restored volume, so the
    alert must say what really happened."""
    result = users_count(tmp_path, script, env, exit_code=124)
    assert result.returncode == 1
    assert "timed out" in result.stderr and "exit 124" in result.stderr
    assert "empty" not in result.stderr


def test_a_killed_count_says_so(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    result = users_count(tmp_path, script, env, exit_code=137)
    assert result.returncode == 1
    assert "exit 137" in result.stderr and "empty" not in result.stderr


def test_a_failed_count_is_not_an_empty_table(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    result = users_count(tmp_path, script, env, answer="could not connect to server", exit_code=2)
    assert result.returncode == 1
    assert "failed (exit 2)" in result.stderr and "empty" not in result.stderr


def stamp(seconds_ago: int) -> str:
    """The UTC date backup.sh would put in a dump's name, `seconds_ago` before now."""
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() - seconds_ago))


def put_dump(
    env: ProductSettings, date_text: str, sidecar: str | None = V2_SIDECAR, file_age_days: int = 0
) -> Path:
    """A dump named for date_text; its file time is set apart from the name on purpose."""
    folder = Path(env.values["BACKUP_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{env.values['BACKUP_PREFIX']}-{date_text}.sql.gz.enc"
    path.write_text("ciphertext")
    file_time = time.time() - file_age_days * 86400
    os.utime(path, (file_time, file_time))
    if sidecar is not None:
        Path(f"{path}.hmac").write_text(sidecar)
    return path


def pick(script: Path, env: ProductSettings, tz: str = "UTC") -> subprocess.CompletedProcess:
    """pick_newest_dump, lifted from the script, with what it chose printed last."""
    code = (
        prelude(script, env)
        + f"{assignment(script, 'REPO')}\n{assignment(script, 'BK')}\n"
        + lift(script, "pick_newest_dump")
        + 'pick_newest_dump\nprintf "chosen=%s\\nepoch=%s\\nnot_v2=%s\\n" "$newest" "$newest_epoch" "$not_v2"\n'
    )
    return subprocess.run(
        ["bash", "-c", code], capture_output=True, text=True, env={**os.environ, "TZ": tz}, check=False
    )


def login_restore(
    tmp_path: Path, script: Path, env: ProductSettings
) -> tuple[subprocess.CompletedProcess, Path]:
    """Step 6 up to the restore.sh call, under a stub restore.sh that records its dump."""
    bin_dir = tmp_path / "bin"
    fake(bin_dir, "docker", "exit 0\n")
    checkout = Path(env.values["REPO_DIR"])
    (checkout / env.values["COMPOSE_DIR"]).mkdir(parents=True)
    called = tmp_path / "restore-called"
    stub = checkout / env.values["LIBRARY_DIR"] / "deploy" / "restore.sh"
    stub.parent.mkdir(parents=True)
    stub.write_text(f'printf %s "$1" >{shlex.quote(str(called))}\n')
    names = ("REPO", "BK", "MAX_DUMP_AGE_HOURS", "FAILED_MARK")
    code = (
        prelude(script, env)
        + "".join(f"{assignment(script, name)}\n" for name in names)
        # The work folder has its own tests; here it must not get in the way.
        + "RESTORE_WORK_DIR=\n"
        + lift(script, "fail")
        + step_6(script)
        # What the cleanup trap reads: a stub that ran is not a failed restore.
        + "RESTORE_OK=1\n"
    )
    result = subprocess.run(
        ["bash", "-c", code], capture_output=True, text=True, env=path_with(bin_dir), check=False
    )
    return result, called


def test_a_touched_older_dump_is_not_chosen_over_a_newer_name(script: Path, env: ProductSettings) -> None:
    """The attack: file times are not authenticated, so `touch` used to select any older dump."""
    older = put_dump(env, stamp(7200))
    newer = put_dump(env, stamp(3600), file_age_days=30)
    os.utime(older)
    result = pick(script, env)
    assert f"chosen={newer}\n" in result.stdout, result.stdout + result.stderr


def test_an_old_name_with_a_fresh_file_time_is_refused_by_the_cap(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    put_dump(env, stamp(72 * 3600))
    result, called = login_restore(tmp_path, script, env)
    assert result.returncode == 1
    assert "newest dump is 72h old (cap 48h)" in result.stderr
    assert not called.exists(), "a stale dump reached restore.sh"


def test_a_recent_name_with_an_ancient_file_time_passes_the_cap(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    dump = put_dump(env, stamp(3600), file_age_days=400)
    result, called = login_restore(tmp_path, script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert called.read_text() == str(dump)
    assert "(1h old)" in result.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "2026101-000000",
        "202610101-000000",
        "20261010-00000",
        "20261010-0000000",
        "20261010000000",
        "abcdefgh-000000",
        "20260230-000000",
        "20261301-000000",
        "20261010-250000",
        "20261010-000000-extra",
        "x20261010-000000",
    ],
)
def test_a_malformed_name_is_skipped_and_logged(script: Path, env: ProductSettings, bad: str) -> None:
    """Valid v2 sidecars, and a sound dump beside them, so the name is the only thing refused."""
    put_dump(env, bad)
    good = put_dump(env, stamp(3600))
    result = pick(script, env)
    assert f"chosen={good}\n" in result.stdout, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("skipping ")]
    assert len(lines) == 1 and f"{env.values['BACKUP_PREFIX']}-{bad}.sql.gz.enc" in lines[0], result.stdout
    assert "not_v2=0\n" in result.stdout, "a bad name was counted as a legacy dump"


def test_only_malformed_names_read_as_no_dumps(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    put_dump(env, "20260230-000000")
    result, called = login_restore(tmp_path, script, env)
    assert result.returncode == 1
    assert "no dumps in" in result.stderr
    assert "skipping" in result.stdout
    assert not called.exists()


def test_a_name_past_the_future_limit_is_skipped_and_logged(script: Path, env: ProductSettings) -> None:
    ahead = put_dump(env, stamp(-900))
    good = put_dump(env, stamp(3600))
    result = pick(script, env)
    assert f"chosen={good}\n" in result.stdout, result.stdout + result.stderr
    assert f"skipping {ahead.name}: its name is dated more than 600s ahead" in result.stdout


def test_a_name_inside_the_future_limit_is_accepted(script: Path, env: ProductSettings) -> None:
    """A little clock skew between the backup and this Mac must not cost a restore."""
    ahead = put_dump(env, stamp(-300))
    put_dump(env, stamp(3600))
    result = pick(script, env)
    assert f"chosen={ahead}\n" in result.stdout, result.stdout + result.stderr
    assert "skipping" not in result.stdout


def test_a_legacy_dump_is_not_chosen_over_an_older_v2_dump(script: Path, env: ProductSettings) -> None:
    put_dump(env, stamp(600), sidecar=LEGACY_SIDECAR)
    v2 = put_dump(env, stamp(7200))
    result = pick(script, env)
    assert f"chosen={v2}\n" in result.stdout, result.stdout + result.stderr
    assert "not_v2=1\n" in result.stdout


@pytest.mark.parametrize("sidecar", [LEGACY_SIDECAR, None])
def test_the_log_names_a_passed_over_dump(script: Path, env: ProductSettings, sidecar: str | None) -> None:
    passed_over = put_dump(env, stamp(600), sidecar=sidecar)
    put_dump(env, stamp(7200))
    result = pick(script, env)
    assert f"passing over {passed_over.name}: legacy sidecar or none\n" in result.stdout, result.stdout


@pytest.mark.parametrize("sidecar", [LEGACY_SIDECAR, None, "v2", "V2 " + "0" * 64, "v3 " + "0" * 64])
def test_only_dumps_without_a_v2_sidecar_name_the_by_hand_route(
    tmp_path: Path, script: Path, env: ProductSettings, sidecar: str | None
) -> None:
    put_dump(env, stamp(3600), sidecar=sidecar)
    result, called = login_restore(tmp_path, script, env)
    assert result.returncode == 1
    assert "1 dump(s)" in result.stderr and "by hand with restore.sh" in result.stderr
    assert "no dumps in" not in result.stderr.replace(env.values["BACKUP_DIR"], "")
    assert not called.exists(), "unattended restore used a dump without a v2 sidecar"


def test_a_symlinked_sidecar_is_not_a_v2_sidecar(tmp_path: Path, script: Path, env: ProductSettings) -> None:
    """restore.sh takes plain files only; choosing this one would only fail later."""
    dump = put_dump(env, stamp(3600), sidecar=None)
    target = tmp_path / "elsewhere"
    target.write_text(V2_SIDECAR)
    Path(f"{dump}.hmac").symlink_to(target)
    result = pick(script, env)
    assert "chosen=\n" in result.stdout and "not_v2=1\n" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("malformed", "ahead", "legacy"),
    [(1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 1, 2)],
    ids=["malformed", "ahead", "legacy", "all"],
)
def test_the_alert_counts_the_dumps_skipped_or_passed_over(
    tmp_path: Path, script: Path, env: ProductSettings, malformed: int, ahead: int, legacy: int
) -> None:
    """The owner sees this at login, without reading the log; it comes before the dump's name, as the
    notification truncates."""
    for name in ["20260230-000000", "x20261010-000000"][:malformed]:
        put_dump(env, name)
    for extra in range(ahead):
        put_dump(env, stamp(-900 - extra))
    for extra in range(legacy):
        put_dump(env, stamp(600 + extra), sidecar=LEGACY_SIDECAR)
    dump = put_dump(env, stamp(3600))
    result, called = login_restore(tmp_path, script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert called.read_text() == str(dump)
    count = malformed + ahead + legacy
    note = f"{count} dump(s) skipped or passed over, see the log"
    assert (
        f"[alert] database volume missing - {note} - auto-restoring {dump.name} (1h old)\n" in result.stderr
    )


def test_the_alert_says_nothing_of_skips_when_there_are_none(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    put_dump(env, stamp(7200))
    dump = put_dump(env, stamp(3600))
    result, _ = login_restore(tmp_path, script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[alert] database volume missing - auto-restoring {dump.name} (1h old)\n" in result.stderr


def test_no_dumps_at_all_keeps_the_no_dumps_message(
    tmp_path: Path, script: Path, env: ProductSettings
) -> None:
    Path(env.values["BACKUP_DIR"]).mkdir(parents=True)
    result, called = login_restore(tmp_path, script, env)
    assert result.returncode == 1
    assert "no dumps in" in result.stderr and "skipping" not in result.stdout
    assert not called.exists()


@macos_only
@pytest.mark.parametrize("tz", ["UTC", "Asia/Kolkata", "America/New_York"])
def test_the_name_is_read_as_utc_in_any_time_zone(script: Path, env: ProductSettings, tz: str) -> None:
    """backup.sh writes the name with `date -u`; read as local time, IST would age it by 5.5 hours."""
    put_dump(env, "20260101-120000")
    result = pick(script, env, tz=tz)
    expected = calendar.timegm((2026, 1, 1, 12, 0, 0))
    assert f"epoch={expected}\n" in result.stdout, result.stdout + result.stderr


def test_a_forged_file_name_logs_one_line_with_its_odd_characters_replaced(
    script: Path, env: ProductSettings
) -> None:
    """The log is the only record of which dump was skipped, so a name must not be able to write a line."""
    prefix = env.values["BACKUP_PREFIX"]
    folder = Path(env.values["BACKUP_DIR"])
    folder.mkdir(parents=True)
    (folder / f"{prefix}-x\nrestored fine\x1b[2K.sql.gz.enc").write_text("ciphertext")
    result = pick(script, env)
    assert result.returncode == 0, result.stderr
    logged = result.stdout.split("chosen=", 1)[0].splitlines()
    assert logged == [
        f"skipping {prefix}-x?restored?fine??2K.sql.gz.enc: its name holds no YYYYMMDD-HHMMSS date"
    ]


def test_a_letter_outside_ascii_is_replaced_in_a_utf_8_locale(
    script: Path, env: ProductSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bash 3.2 reads a bracket range by the locale's collation, so an é falls inside A-Za-z there."""
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    prefix = env.values["BACKUP_PREFIX"]
    folder = Path(env.values["BACKUP_DIR"])
    folder.mkdir(parents=True)
    (folder / f"{prefix}-é.sql.gz.enc").write_text("ciphertext")
    result = pick(script, env)
    assert result.returncode == 0, result.stderr
    logged = result.stdout.split("chosen=", 1)[0].splitlines()
    assert logged == [f"skipping {prefix}-??.sql.gz.enc: its name holds no YYYYMMDD-HHMMSS date"]


def test_no_skip_line_prints_the_raw_name(script: Path) -> None:
    """The other two skip lines need a name that already passed the pattern, so they are read, not run."""
    function = lift(script, "pick_newest_dump")
    assert function.count('echo "skipping ${safe_name}:') == 3 and "skipping ${name}" not in function


def test_nothing_in_step_6_reads_a_file_time(script: Path) -> None:
    """Runs everywhere, so a Linux CI still notices the old selection coming back."""
    chosen = step_6(script).split('alert "database volume missing', 1)[0]
    for reader in ("ls -t", "stat ", "-mtime", "-newer"):
        assert reader not in chosen, f"step 6 reads a file time again: {reader}"
