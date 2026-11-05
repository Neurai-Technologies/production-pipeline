"""A backup must be tamper-evident, and a restore must prove the file whole before psql sees it.

backup.sh and restore.sh run in different places (the backup container, the
host), so they each carry the same key derivation; the first tests pin them to
each other. The rest drive the real scripts with the real openssl and gzip, a
fake pg_dump, and a fake docker that records what would have reached psql.
backup.sh reads the product settings from its environment (env_file:
product.settings); restore.sh sources the product.settings PRODUCT_SETTINGS names.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import LIBRARY_ROOT, ProductSettings
from tests.shell import fake, lift

BACKUP = LIBRARY_ROOT / "deploy" / "backup" / "backup.sh"
RESTORE = LIBRARY_ROOT / "deploy" / "restore.sh"
CRYPTO = BACKUP.with_name("crypto.sh")
PASSPHRASE = "correct horse battery staple"
# The erasure query the tests configure, and what the fake docker answers it
# with, so a test can assert the value reached the warning rather than that
# some text did.
ERASURE_SQL = "select max(created_at) from deletion_requests"
FAKE_ERASURE_AT = "2026-09-11 04:00:00+00"
# Shaped like real pg_dump 16 output, including the \restrict / \unrestrict
# lines newer releases add, and incompressible enough that the gzipped,
# encrypted dump clears backup.sh's 1KB floor.
FAKE_DUMP = (
    "--\n-- PostgreSQL database dump\n--\n\n\\restrict 27aJ1fET0VNu2nqqzdCOrNDKzrHcPsXLtQ\n\n"
    "-- Dumped from database version 16.15\n\n"
    + "".join(
        f"INSERT INTO accounts VALUES ({i}, '{hashlib.sha256(bytes([i])).hexdigest()}');\n" for i in range(80)
    )
    + "\n--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict 27aJ1fET0VNu2nqqzdCOrNDKzrHcPsXLtQ\n\n"
)


def hmac_via(script: Path, path: Path, salt: str) -> str:
    assert "crypto.sh" in script.read_text(encoding="utf-8")
    code = f". {shlex.quote(str(CRYPTO))}\n_backup_crypto_hmac {shlex.quote(str(path))}"
    return subprocess.check_output(
        ["bash", "-c", code],
        text=True,
        env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE, "BACKUP_HMAC_SALT": salt},
    ).strip()


def v2_sidecar(dump: Path, salt: str) -> str:
    """A dump's v2 sidecar, computed independently of crypto.sh: label line, name line, ciphertext."""
    key = hashlib.pbkdf2_hmac("sha256", PASSPHRASE.encode(), salt.encode(), 10000, dklen=48)[:32]
    message = b"backup-crypto-v2\n" + dump.name.encode() + b"\n" + dump.read_bytes()
    return f"v2 {hmac.new(key, message, 'sha256').hexdigest()}\n"


@pytest.fixture
def env(product_settings: Callable[..., ProductSettings]) -> ProductSettings:
    """A product with an erasure record to check after a restore."""
    return product_settings(ERASURE_CHECK_SQL=ERASURE_SQL)


def dumps(root: Path, env: ProductSettings, suffix: str = "") -> list[Path]:
    return sorted(root.glob(f"{env.values['BACKUP_PREFIX']}-*.sql.gz.enc{suffix}"))


def run_backup(
    tmp_path: Path,
    env: ProductSettings,
    dump_fails: bool = False,
    args: tuple[str, ...] = (),
    check: bool = True,
    **extra_env: str,
) -> tuple[Path, subprocess.CompletedProcess]:
    """Run backup.sh once for real: fake pg_dump, real gzip and openssl, a fake
    curl that records what it would have uploaded, and a fake sleep that ends
    the loop after the first dump (and leaves a `slept` file, so a test can
    tell the loop from `once`) unless the test made its own. args are passed
    to backup.sh; check=False skips the written/FAILED assertion.

    dump_fails drives the case retention was blind to: the database is too
    broken to dump, so the success branch never runs.
    """
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "curl-calls"
    fake(
        bin_dir,
        "pg_dump",
        "echo 'pg_dump: connection failed' >&2\nexit 1\n" if dump_fails else "printf '%s' \"$FAKE_DUMP\"\n",
    )
    if not (bin_dir / "sleep").exists():
        fake(bin_dir, "sleep", f'touch "{tmp_path / "slept"}"\nkill $PPID\n')
    if not (bin_dir / "curl").exists():
        fake(
            bin_dir,
            "curl",
            f'printf "argv: %s\\n" "$*" >>"{calls}"\nprintf "stdin: %s\\n" "$(cat)" >>"{calls}"\n',
        )
    out = tmp_path / "backups"
    out.mkdir(exist_ok=True)
    # The backup-lock volume compose mounts at /lock.
    lock_dir = tmp_path / "lock-volume"
    lock_dir.mkdir(exist_ok=True)
    run_env = {
        **os.environ,
        **env.values,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BACKUP_PASSPHRASE": PASSPHRASE,
        "BACKUP_ROOT": str(out),
        "BACKUP_HEARTBEAT": str(tmp_path / "no-heartbeat-dir" / "last-success"),
        "BACKUP_LOCK_DIR": str(lock_dir),
        "FAKE_DUMP": FAKE_DUMP,
        **extra_env,
    }
    # bash, not sh: the container runs busybox ash, which accepts the script's
    # pipefail; the Ubuntu runner's sh is dash, which does not.
    result = subprocess.run(
        ["bash", str(BACKUP), *args], env=run_env, capture_output=True, text=True, check=False, timeout=60
    )
    if not check:
        pass
    elif dump_fails:
        assert "backup FAILED" in result.stderr, result.stdout
    else:
        assert "backup written" in result.stdout, result.stderr
    return out, result


@pytest.fixture
def backup_dir(tmp_path: Path, env: ProductSettings) -> Path:
    return run_backup(tmp_path, env)[0]


def restore(
    tmp_path: Path,
    env: ProductSettings,
    dump: Path,
    path_prefix: str = "",
    erasure_out: str = FAKE_ERASURE_AT,
    erasure_err: str = "",
    erasure_code: int = 0,
    break_notice: bool = False,
    break_cleanup: bool = False,
    close_stdout: bool = False,
    **extra_env: str,
) -> tuple[subprocess.CompletedProcess, Path]:
    bin_dir = tmp_path / "restore-bin"
    received = tmp_path / "psql-stdin"
    # restore.sh calls docker twice: the piped psql that applies the dump, and
    # a read-only query (-tAc) for the erasure boundary. They are told apart by
    # argv rather than by whether stdin happens to be empty - the query must
    # ANSWER, and it must not read stdin at all (it runs inside $( ) with no
    # redirect, so it inherits the script's fd 0 and would block for ever
    # under `pytest -s`).
    fake(
        bin_dir,
        "docker",
        f'printf "argv: %s\\n" "$*" >>"{received}.argv"\n'
        'case " $* " in *" -tAc "*)'
        # What is still in TMPDIR at boundary time. The decrypted dump is the
        # whole database in cleartext and must already be gone; the file the
        # boundary writes its stderr to is named after it, so the assertion is
        # about the suffix rather than the glob.
        f' ls "$TMPDIR"/{env.values["PRODUCT"]}-restore.* >"{received}.tmpfiles" 2>/dev/null || true;'
        f" printf '%s' {shlex.quote(erasure_out)};"
        f" printf '%s' {shlex.quote(erasure_err)} >&2;"
        f" exit {erasure_code};; esac\n"
        f'/bin/cat >"{received}"\n',
    )
    run_env = {
        **os.environ,
        "PATH": ":".join(p for p in (str(bin_dir), path_prefix, os.environ["PATH"]) if p),
        "PRODUCT_SETTINGS": str(env.path),
        "BACKUP_PASSPHRASE": PASSPHRASE,
        "TMPDIR": str(tmp_path),
        **extra_env,
    }
    # A write that fails after the database has been written. The notice is a
    # heredoc through `cat`, so a cat that refuses is exactly that failure,
    # and it reaches nothing else: the fake docker above calls /bin/cat.
    if break_notice:
        fake(bin_dir, "cat", '[ "$#" -ne 0 ] || exit 1\nexec /bin/cat "$@"\n')
    # An unlink that fails - a read-only or full filesystem, which is the
    # condition a restore runs under. rm -f returns 0 for a MISSING file, so
    # only a stub reproduces the case the guards exist for.
    if break_cleanup:
        fake(bin_dir, "rm", 'case " $* " in *"-restore."*) exit 1 ;; esac\nexec /bin/rm "$@"\n')
    # fd 1 closed. The completion echo is the script's only stdout write, so
    # this fails exactly that one command and nothing else.
    streams: dict[str, object] = (
        {"stderr": subprocess.PIPE, "preexec_fn": lambda: os.close(1)}
        if close_stdout
        else {"capture_output": True}
    )
    result = subprocess.run(["bash", str(RESTORE), str(dump)], env=run_env, text=True, check=False, **streams)
    return result, received


def plaintext_left(tmp_path: Path, env: ProductSettings) -> list[Path]:
    return list(tmp_path.glob(f"{env.values['PRODUCT']}-restore.*"))


def aged_dump(root: Path, days: int, name: str) -> Path:
    """A dump file that looks `days` old to find(1)."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"x" * 2048)
    old = time.time() - days * 86400
    os.utime(path, (old, old))
    return path


def test_a_failed_dump_still_expires_what_is_past_the_ceiling(tmp_path: Path, env: ProductSettings) -> None:
    """Retention used to live only in the success branch, so a database too
    broken to dump was one whose old dumps stopped expiring - personal data
    outliving the published window in the one case nobody is watching."""
    root = tmp_path / "backups"
    prefix = env.values["BACKUP_PREFIX"]
    stale = aged_dump(root, 60, f"{prefix}-20260722-000000.sql.gz.enc")
    stale_sidecar = aged_dump(root, 60, f"{prefix}-20260722-000000.sql.gz.enc.hmac")
    recent = aged_dump(root, 3, f"{prefix}-20260918-000000.sql.gz.enc")

    run_backup(tmp_path, env, dump_fails=True)

    assert not stale.exists(), "a dump past the ceiling survived a failed run"
    assert not stale_sidecar.exists(), "the sidecar outlived its dump"
    assert recent.exists(), "the ceiling took a dump that was well inside it"


def test_the_ceiling_leaves_the_operator_room_to_notice(tmp_path: Path, env: ProductSettings) -> None:
    """The ordinary window is 14 days and the ceiling is 55. A month of failed
    dumps must not clear the history out: that is the case where the operator
    most needs something to restore from."""
    month_old = aged_dump(
        tmp_path / "backups", 30, f"{env.values['BACKUP_PREFIX']}-20260822-000000.sql.gz.enc"
    )

    run_backup(tmp_path, env, dump_fails=True)

    assert month_old.exists(), "a month of failures left nothing to restore from"


def test_a_good_dump_still_prunes_at_the_ordinary_window(tmp_path: Path, env: ProductSettings) -> None:
    """The second prune must not have replaced the first one."""
    root = tmp_path / "backups"
    prefix = env.values["BACKUP_PREFIX"]
    past_window = aged_dump(root, 20, f"{prefix}-20260901-000000.sql.gz.enc")
    inside = aged_dump(root, 5, f"{prefix}-20260916-000000.sql.gz.enc")

    run_backup(tmp_path, env)

    assert not past_window.exists(), "the ordinary retention stopped pruning"
    assert inside.exists()


def test_retention_only_touches_this_products_dumps(tmp_path: Path, env: ProductSettings) -> None:
    """The prunes match BACKUP_PREFIX, so another file in the directory is not theirs to delete."""
    other = aged_dump(tmp_path / "backups", 60, "otherprefix-20260722-000000.sql.gz.enc")

    run_backup(tmp_path, env)

    assert other.exists()


def test_a_verified_dump_touches_the_heartbeat(tmp_path: Path, env: ProductSettings) -> None:
    """The worker's alarm reads this file's age; it must be written only once
    the dump has passed the size and HMAC checks, so a heartbeat means a
    restorable backup and not merely an attempt."""
    beat = tmp_path / "heartbeat" / "last-success"
    beat.parent.mkdir()

    run_backup(tmp_path, env, BACKUP_HEARTBEAT=str(beat))

    assert beat.exists()
    assert beat.read_text().strip().endswith("Z"), "an ISO-8601 UTC stamp, readable by a person too"


def test_a_failed_dump_leaves_the_heartbeat_untouched(tmp_path: Path, env: ProductSettings) -> None:
    beat = tmp_path / "heartbeat" / "last-success"
    beat.parent.mkdir()

    run_backup(tmp_path, env, dump_fails=True, BACKUP_HEARTBEAT=str(beat))

    assert not beat.exists(), "a failed dump must not look like a fresh backup"


def test_a_heartbeat_that_cannot_be_written_does_not_fail_the_backup(
    tmp_path: Path, env: ProductSettings
) -> None:
    """The dump is already safe by then; the alarm side is worth a line, not a lost backup."""
    out, result = run_backup(tmp_path, env)

    assert dumps(out, env), "the dump itself must still land"
    assert "heartbeat not written" in result.stderr


def test_once_runs_one_cycle_and_exits_0(tmp_path: Path, env: ProductSettings) -> None:
    """The deployer's PRE_DEPLOY_HOOK runs `backup once`: the same dump, sidecar
    and heartbeat as a scheduled cycle, then an exit instead of the sleep."""
    beat = tmp_path / "heartbeat" / "last-success"
    beat.parent.mkdir()

    out, result = run_backup(tmp_path, env, args=("once",), BACKUP_HEARTBEAT=str(beat))

    assert result.returncode == 0, result.stderr
    found = dumps(out, env)
    assert len(found) == 1
    sidecar = Path(f"{found[0]}.hmac")
    assert sidecar.read_text() == v2_sidecar(found[0], env.values["BACKUP_HMAC_SALT"])
    assert beat.exists()
    assert not (tmp_path / "slept").exists(), "once must exit, not wait for the next cycle"


def test_once_with_a_failed_dump_exits_non_zero(tmp_path: Path, env: ProductSettings) -> None:
    """A failing PRE_DEPLOY_HOOK fails the deploy, so the failure must reach the exit status."""
    out, result = run_backup(tmp_path, env, dump_fails=True, args=("once",))

    assert result.returncode != 0
    assert not dumps(out, env) and not dumps(out, env, ".hmac")
    assert not (tmp_path / "slept").exists()


def test_once_still_prunes_at_both_windows(tmp_path: Path, env: ProductSettings) -> None:
    root = tmp_path / "backups"
    prefix = env.values["BACKUP_PREFIX"]
    past_window = aged_dump(root, 20, f"{prefix}-20260901-000000.sql.gz.enc")
    inside = aged_dump(root, 5, f"{prefix}-20260916-000000.sql.gz.enc")

    run_backup(tmp_path, env, args=("once",))

    assert not past_window.exists()
    assert inside.exists()


def hold_lock(root: Path) -> subprocess.Popen:
    """Another cycle holding the backup lock, as flock(1) would take it: in
    run_backup's lock folder, the volume both runs mount."""
    root.mkdir(exist_ok=True)
    lock_dir = root.parent / "lock-volume"
    lock_dir.mkdir(exist_ok=True)
    code = (
        "import fcntl, time\n"
        f"f = open({str(lock_dir / 'backup.lock')!r}, 'a')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
    return holder


def test_the_lock_lives_on_its_own_volume_not_beside_the_dumps(tmp_path: Path, env: ProductSettings) -> None:
    """The dumps sit on a host folder bind-mounted into the VM, where flock is
    unproven; the lock sits on the backup-lock volume, at /lock by default."""
    out, _result = run_backup(tmp_path, env, args=("once",))

    assert (tmp_path / "lock-volume" / "backup.lock").is_file()
    assert [p.name for p in out.iterdir() if "lock" in p.name] == []
    assert 'LOCK_DIR="${BACKUP_LOCK_DIR:-/lock}"' in BACKUP.read_text(encoding="utf-8")


@pytest.mark.parametrize("args", [(), ("once",)])
def test_a_missing_lock_volume_is_refused_before_any_dump(
    tmp_path: Path, env: ProductSettings, args: tuple[str, ...]
) -> None:
    """Refused, never moved: a lock anywhere else would not be shared."""
    missing = tmp_path / "no-lock-volume"
    out, result = run_backup(tmp_path, env, args=args, check=False, BACKUP_LOCK_DIR=str(missing))

    assert result.returncode == 1
    assert f"backup: {missing} missing or not writable (the cycle lock lives there)" in result.stderr
    assert (
        "add backup-lock:/lock to the backup service's volumes and declare backup-lock: under the top-level volumes"
        in result.stderr
    )
    assert not dumps(out, env) and not dumps(out, env, ".hmac")
    assert "backup written" not in result.stdout


def test_once_waits_for_a_cycle_holding_the_lock(tmp_path: Path, env: ProductSettings) -> None:
    """The scheduled service and a pre-deploy `once` share the directory; the
    second waits for the first to finish instead of writing beside it."""
    holder = hold_lock(tmp_path / "backups")
    try:
        # The first wait ends the other cycle; its lock goes with it, so a
        # later try takes the lock. (Not kill -0: the holder stays a zombie
        # until this test reaps it.)
        fake(
            tmp_path / "bin",
            "sleep",
            f'touch "{tmp_path / "waited"}"\nkill {holder.pid}\n/bin/sleep 0.1\n',
        )
        out, result = run_backup(tmp_path, env, args=("once",))
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "waited").exists(), "once ran without waiting for the lock"
    assert len(dumps(out, env)) == 1


def test_once_gives_up_after_the_bounded_wait(tmp_path: Path, env: ProductSettings) -> None:
    """The .part of the cycle holding the lock is not swept, and the deploy hears the failure."""
    root = tmp_path / "backups"
    holder = hold_lock(root)
    writing = root / f"{env.values['BACKUP_PREFIX']}-20260916-000000.sql.gz.enc.part"
    writing.write_bytes(b"x")
    crypto_work = root / ".backup-crypto.active"
    crypto_work.mkdir()
    (crypto_work / "ciphertext").write_bytes(b"active ciphertext")
    try:
        fake(tmp_path / "bin", "sleep", f'echo "$1" >>"{tmp_path / "waits"}"\n')
        out, result = run_backup(tmp_path, env, args=("once",), check=False)
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode != 0
    assert "this cycle skipped" in result.stderr
    assert (tmp_path / "waits").read_text().split() == ["5"] * 59, "60 tries, 5 s apart"
    assert writing.exists(), "the sweep deleted the .part of the cycle holding the lock"
    assert (crypto_work / "ciphertext").read_bytes() == b"active ciphertext"
    assert not dumps(out, env)


def test_the_loop_skips_a_cycle_while_the_lock_is_held(tmp_path: Path, env: ProductSettings) -> None:
    root = tmp_path / "backups"
    holder = hold_lock(root)
    writing = root / f"{env.values['BACKUP_PREFIX']}-20260916-000000.sql.gz.enc.part"
    writing.write_bytes(b"x")
    try:
        # Lock waits return at once; the 6 h sleep ends the run.
        fake(tmp_path / "bin", "sleep", '[ "$1" = 21600 ] && kill $PPID\nexit 0\n')
        out, result = run_backup(tmp_path, env, check=False)
    finally:
        holder.kill()
        holder.wait()

    assert "this cycle skipped" in result.stderr
    assert writing.exists(), "the loop's sweep ran without the lock"
    assert not dumps(out, env)


def test_without_an_argument_backup_keeps_looping(tmp_path: Path, env: ProductSettings) -> None:
    """No argument is the long-lived service: after the first cycle it sleeps
    (the fake sleep then ends the run) and clears stale .part files at start."""
    root = tmp_path / "backups"
    root.mkdir()
    stale = root / f"{env.values['BACKUP_PREFIX']}-20260916-000000.sql.gz.enc.part"
    stale.write_bytes(b"x")

    out, _result = run_backup(tmp_path, env)

    assert (tmp_path / "slept").exists()
    assert not stale.exists()
    assert len(dumps(out, env)) == 1


@pytest.mark.parametrize("args", [(), ("once",)])
def test_locked_backup_sweep_removes_abandoned_crypto_work(
    tmp_path: Path, env: ProductSettings, args: tuple[str, ...]
) -> None:
    root = tmp_path / "backups"
    stale = root / ".backup-crypto.abandoned"
    stale.mkdir(parents=True)
    (stale / "ciphertext").write_bytes(b"interrupted ciphertext")
    unrelated = root / "keep"
    unrelated.mkdir()
    (unrelated / "ciphertext").write_bytes(b"keep")
    run_backup(tmp_path, env, args=args)
    assert not stale.exists()
    assert (unrelated / "ciphertext").read_bytes() == b"keep"


@pytest.mark.parametrize("arg", ["twice", "--once", "ONCE"])
def test_backup_refuses_any_other_argument(tmp_path: Path, env: ProductSettings, arg: str) -> None:
    bin_dir = tmp_path / "bin"
    fake(bin_dir, "pg_dump", f'touch "{tmp_path / "dumped"}"\n')
    fake(bin_dir, "sleep", "kill $PPID\n")
    result = subprocess.run(
        ["bash", str(BACKUP), arg],
        env={
            **os.environ,
            **env.values,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BACKUP_PASSPHRASE": PASSPHRASE,
            "BACKUP_ROOT": str(tmp_path / "backups"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode != 0
    assert "usage: backup.sh [once]" in result.stderr
    assert not (tmp_path / "dumped").exists()


@pytest.mark.parametrize("args", [(), ("once",)])
def test_backup_refuses_to_start_without_flock(
    tmp_path: Path, env: ProductSettings, args: tuple[str, ...]
) -> None:
    """lock() cannot tell a missing flock from a busy lock, so without this
    check every cycle would be skipped as held by another one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Only what the script needs before the check, and pg_dump to prove no dump ran.
    openssl = shutil.which("openssl")
    assert openssl
    (bin_dir / "openssl").symlink_to(openssl)
    fake(bin_dir, "pg_dump", f'touch "{tmp_path / "dumped"}"\n')
    bash = shutil.which("bash")
    assert bash
    result = subprocess.run(
        [bash, str(BACKUP), *args],
        env={
            **env.values,
            "PATH": str(bin_dir),
            "BACKUP_PASSPHRASE": PASSPHRASE,
            "BACKUP_ROOT": str(tmp_path / "backups"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode != 0
    assert "flock is missing from this image" in result.stderr, result.stderr
    assert not (tmp_path / "dumped").exists()
    assert not (tmp_path / "backups").exists()


def test_backup_and_restore_derive_the_same_mac(tmp_path: Path) -> None:
    sample = tmp_path / "sample"
    sample.write_bytes(b"ciphertext")
    assert hmac_via(BACKUP, sample, "fakesalt") == hmac_via(RESTORE, sample, "fakesalt")
    assert len(hmac_via(BACKUP, sample, "fakesalt")) == 64


def test_the_salt_keys_the_mac(tmp_path: Path) -> None:
    """BACKUP_HMAC_SALT is a product setting now; a different salt must be a different key."""
    sample = tmp_path / "sample"
    sample.write_bytes(b"ciphertext")
    assert hmac_via(BACKUP, sample, "fakesalt") != hmac_via(BACKUP, sample, "othersal")


def test_openssl_keeps_only_8_bytes_of_the_salt() -> None:
    """The positive control for test_backup_salt: openssl's -S takes at most
    8 bytes (16 hex digits) and drops the rest with only a "hex string is too
    long" warning, so two salts that share their first 8 bytes derive one
    key. That is why the scripts refuse a BACKUP_HMAC_SALT that is not
    exactly 8 bytes. Run against openssl itself, the derivation the scripts
    use, so the control holds wherever the scripts put their check."""

    def key(salt: str) -> str:
        done = subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-pass",
                "env:BACKUP_PASSPHRASE",
                "-S",
                salt.encode().hex(),
                "-P",
            ],
            env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE},
            capture_output=True,
            text=True,
            check=True,
        )
        (line,) = [x for x in done.stdout.splitlines() if x.startswith("key=")]
        return line

    assert key("salt-at-backup-time") == key("salt-at-restore-time"), "openssl now keys on the whole salt"
    assert key("fakesalt") != key("othersal")


def test_every_script_requires_the_salt_and_derives_its_hex_the_same_way() -> None:
    """BACKUP_HMAC_SALT is a required product setting with no default: a
    default in one script and not another would key the sidecars differently
    on each side. The hex for openssl's -S is derived by one identical
    expression in all three, or a dump sealed by one is refused by another."""
    required = "BACKUP_HMAC_SALT:?"
    derivation = "printf '%s' \"$BACKUP_HMAC_SALT\" | od -An -tx1 | tr -d ' \\n'"
    for script in (BACKUP, RESTORE, BACKUP.with_name("logarchive.sh")):
        text = script.read_text(encoding="utf-8")
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        assert any(line.startswith(': "${' + required) for line in code), (
            f"{script.name} does not require the salt"
        )
        assert not any("BACKUP_HMAC_SALT:-" in line for line in code), f"{script.name} defaults the salt"
        assert not any(line.startswith("BACKUP_HMAC_SALT=") for line in code), (
            f"{script.name} assigns the salt"
        )
        assert "crypto.sh" in text
        assert "mac_key()" not in text
    assert lift(CRYPTO, "_backup_crypto_hmac").count(derivation) == 1


def test_backup_writes_a_dump_with_a_matching_sidecar(backup_dir: Path, env: ProductSettings) -> None:
    found = dumps(backup_dir, env)
    assert len(found) == 1
    sidecar = Path(f"{found[0]}.hmac")
    assert sidecar.read_text() == v2_sidecar(found[0], env.values["BACKUP_HMAC_SALT"])
    assert not list(backup_dir.glob("*.part*"))


def test_restore_feeds_psql_only_a_verified_whole_dump(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    result, received = restore(tmp_path, env, dumps(backup_dir, env)[0])
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP
    argv = Path(f"{received}.argv").read_text()
    assert "--single-transaction" in argv
    assert f"-p {env.values['COMPOSE_PROJECT']} exec -T {env.values['DB_SERVICE']} psql" in argv, argv
    assert not plaintext_left(tmp_path, env), "plaintext temp file must be removed"


def test_restore_warns_once_on_a_legacy_dump_and_still_restores_it(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    dump = dumps(backup_dir, env)[0]
    legacy = hmac_via(BACKUP, dump, env.values["BACKUP_HMAC_SALT"])
    Path(f"{dump}.hmac").write_text(f"{legacy}\n")
    result, received = restore(tmp_path, env, dump)
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP
    warnings = [line for line in result.stderr.splitlines() if "old format" in line]
    assert len(warnings) == 1, result.stderr


def test_restore_refuses_a_dump_sealed_under_another_salt(tmp_path: Path, product_settings: Callable) -> None:
    """A product.settings whose salt differs from the one the dump was sealed under is a refusal, not a restore."""
    sealed = product_settings(BACKUP_HMAC_SALT="backupsl")
    out, _ = run_backup(tmp_path, sealed)
    reading = product_settings(BACKUP_HMAC_SALT="restorsl")
    result, received = restore(tmp_path, reading, dumps(out, reading)[0])
    assert result.returncode == 1 and "does not match its .hmac sidecar" in result.stderr
    assert "REFUSED:" in result.stderr
    assert not received.exists()


def test_restore_refuses_a_modified_dump_before_decrypting(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    dump = dumps(backup_dir, env)[0]
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0x01
    dump.write_bytes(data)
    result, received = restore(tmp_path, env, dump)
    assert result.returncode == 1 and "does not match its .hmac sidecar" in result.stderr
    assert "REFUSED:" in result.stderr
    assert not received.exists()


def test_restore_refuses_a_wrong_passphrase_before_decrypting(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    result, received = restore(tmp_path, env, dumps(backup_dir, env)[0], BACKUP_PASSPHRASE="not it")
    assert result.returncode == 1 and "wrong passphrase" in result.stderr
    assert "REFUSED:" in result.stderr
    assert not received.exists()


def test_restore_refuses_a_dump_without_a_sidecar_unless_told(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    dump = dumps(backup_dir, env)[0]
    Path(f"{dump}.hmac").unlink()
    result, received = restore(tmp_path, env, dump)
    assert result.returncode == 1 and "RESTORE_UNVERIFIED=1" in result.stderr
    assert not received.exists()
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="1")
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP


def test_a_required_v2_refuses_a_legacy_dump_that_verifies(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """The login's restore: a legacy pair put under the chosen name verifies, since legacy binds no name."""
    dump = dumps(backup_dir, env)[0]
    legacy = hmac_via(BACKUP, dump, env.values["BACKUP_HMAC_SALT"])
    Path(f"{dump}.hmac").write_text(f"{legacy}\n")
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="", RESTORE_REQUIRE_V2="1")
    assert result.returncode == 1 and "REFUSED:" in result.stderr
    assert "requires a v2 .hmac sidecar" in result.stderr and "old format" not in result.stderr
    assert not received.exists(), "a legacy dump reached psql"
    assert not plaintext_left(tmp_path, env)


def test_a_required_v2_accepts_a_v2_dump(tmp_path: Path, backup_dir: Path, env: ProductSettings) -> None:
    dump = dumps(backup_dir, env)[0]
    assert Path(f"{dump}.hmac").read_text().startswith("v2 ")
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="", RESTORE_REQUIRE_V2="1")
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP


def test_a_required_v2_cannot_be_opened_by_restore_unverified(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    dump = dumps(backup_dir, env)[0]
    Path(f"{dump}.hmac").unlink()
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="1", RESTORE_REQUIRE_V2="1")
    assert result.returncode == 1 and "REFUSED:" in result.stderr
    assert "RESTORE_UNVERIFIED=1" not in result.stderr, "the refusal offers an opt-in that does not apply"
    assert not received.exists(), "a dump with no sidecar reached psql"
    assert not plaintext_left(tmp_path, env)


def test_an_empty_require_v2_leaves_the_by_hand_restore_as_it_was(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    dump = dumps(backup_dir, env)[0]
    legacy = hmac_via(BACKUP, dump, env.values["BACKUP_HMAC_SALT"])
    Path(f"{dump}.hmac").write_text(f"{legacy}\n")
    result, received = restore(tmp_path, env, dump, RESTORE_REQUIRE_V2="")
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP
    assert result.stderr.count("old format") == 1, result.stderr
    received.unlink()
    Path(f"{dump}.hmac").unlink()
    result, received = restore(tmp_path, env, dump, RESTORE_REQUIRE_V2="")
    assert result.returncode == 1 and "RESTORE_UNVERIFIED=1" in result.stderr
    assert not received.exists()
    result, received = restore(tmp_path, env, dump, RESTORE_REQUIRE_V2="", RESTORE_UNVERIFIED="1")
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP


def settings_also_say(env: ProductSettings, lines: str) -> None:
    with env.path.open("a") as settings:
        settings.write(lines)


def test_a_settings_line_setting_the_flags_does_not_change_what_an_automatic_restore_set(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """vm-autostart passes these two; a line naming them in the settings file does not replace them."""
    settings_also_say(env, "RESTORE_REQUIRE_V2=\nRESTORE_UNVERIFIED=1\n")
    dump = dumps(backup_dir, env)[0]
    Path(f"{dump}.hmac").write_text(f"{hmac_via(BACKUP, dump, env.values['BACKUP_HMAC_SALT'])}\n")
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="", RESTORE_REQUIRE_V2="1")
    assert result.returncode == 1 and "requires a v2 .hmac sidecar" in result.stderr
    assert not received.exists(), "a legacy dump reached psql"
    Path(f"{dump}.hmac").unlink()
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="", RESTORE_REQUIRE_V2="1")
    assert result.returncode == 1 and "requires a v2 sidecar" in result.stderr
    assert not received.exists(), "a dump with no sidecar reached psql"
    assert not plaintext_left(tmp_path, env)


def test_a_settings_line_setting_the_flag_does_not_open_a_restore_by_hand_that_set_nothing(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    settings_also_say(env, "RESTORE_UNVERIFIED=1\n")
    dump = dumps(backup_dir, env)[0]
    Path(f"{dump}.hmac").unlink()
    result, received = restore(tmp_path, env, dump)
    assert result.returncode == 1 and "RESTORE_UNVERIFIED=1" in result.stderr
    assert not received.exists(), "a dump with no sidecar reached psql"


def test_a_settings_line_setting_the_flags_does_not_refuse_what_a_restore_by_hand_set(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """The caller's own values hold in the other direction too."""
    settings_also_say(env, "RESTORE_REQUIRE_V2=1\nRESTORE_UNVERIFIED=\n")
    dump = dumps(backup_dir, env)[0]
    Path(f"{dump}.hmac").unlink()
    result, received = restore(tmp_path, env, dump, RESTORE_UNVERIFIED="1")
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP


def test_restore_cannot_fall_back_when_a_checked_sidecar_disappears(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    dump = dumps(backup_dir, env)[0]
    shim = tmp_path / "shim"
    mktemp = shutil.which("mktemp")
    assert mktemp
    # This is after the initial sidecar check, before the old second check.
    fake(shim, "mktemp", f'rm -f {shlex.quote(str(dump) + ".hmac")}\nexec {shlex.quote(mktemp)} "$@"\n')
    result, received = restore(tmp_path, env, dump, path_prefix=str(shim), RESTORE_UNVERIFIED="0")
    assert result.returncode == 1 and "REFUSED:" in result.stderr
    assert not received.exists(), "a disappearing sidecar silently selected unverified decryption"
    assert not plaintext_left(tmp_path, env)


def test_restore_refuses_a_truncated_dump(tmp_path: Path, env: ProductSettings) -> None:
    truncated = FAKE_DUMP.split("-- PostgreSQL database dump complete")[0]
    dump = tmp_path / f"{env.values['BACKUP_PREFIX']}-truncated.sql.gz.enc"
    code = (
        f". {shlex.quote(str(CRYPTO))}\n"
        f"printf '%s' \"$FAKE_DUMP\" | gzip | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out {dump}\n"
        f"_backup_crypto_hmac {dump} >{dump}.hmac"
    )
    subprocess.run(
        ["bash", "-c", code],
        env={
            **os.environ,
            "BACKUP_PASSPHRASE": PASSPHRASE,
            "BACKUP_HMAC_SALT": env.values["BACKUP_HMAC_SALT"],
            "FAKE_DUMP": truncated,
        },
        check=True,
    )
    result, received = restore(tmp_path, env, dump)
    assert result.returncode == 1 and "truncated" in result.stderr
    assert not received.exists()
    # The refusal happens AFTER the decrypt, so this is the failure path that
    # leaves the whole database in cleartext if the EXIT trap stops carrying
    # it. The success path removes the plaintext explicitly the moment psql
    # has read it, so the trap is the only thing covering this one.
    assert not plaintext_left(tmp_path, env), "a refused restore left the decrypted dump on disk"


def test_restore_refuses_when_the_mac_key_cannot_be_derived(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """An openssl that prints nothing for the key must not let an empty sidecar through."""
    dump = dumps(backup_dir, env)[0]
    Path(f"{dump}.hmac").write_text("0" * 64 + "\n")
    shim_dir = tmp_path / "shim"
    fake(
        shim_dir, "openssl", f'case " $* " in *" -P "*) exit 0 ;; esac\nexec {shutil.which("openssl")} "$@"\n'
    )
    result, received = restore(tmp_path, env, dump, path_prefix=str(shim_dir))
    assert result.returncode == 1 and "could not derive the MAC key" in result.stderr
    assert not received.exists()


R2 = {
    "BACKUP_R2_ENDPOINT": "https://storage.invalid",
    "BACKUP_R2_BUCKET": "fake-bucket",
    "BACKUP_R2_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "BACKUP_R2_SECRET_ACCESS_KEY": "s3cr3t-example",
}


def test_without_r2_settings_nothing_is_uploaded(tmp_path: Path, env: ProductSettings) -> None:
    _out, result = run_backup(tmp_path, env)
    assert "off-site copies off" in result.stdout
    assert not (tmp_path / "curl-calls").exists()


def test_dump_and_sidecar_are_uploaded_with_credentials_by_stdin(
    tmp_path: Path, env: ProductSettings
) -> None:
    out, result = run_backup(tmp_path, env, **R2)
    dump = dumps(out, env)[0]
    lines = (tmp_path / "curl-calls").read_text().splitlines()
    argv = [line for line in lines if line.startswith("argv:")]
    stdin = [line for line in lines if line.startswith("stdin:")]
    assert len(argv) == 2 and len(stdin) == 2
    assert argv[0].endswith(f"{R2['BACKUP_R2_ENDPOINT']}/{R2['BACKUP_R2_BUCKET']}/{dump.name}")
    assert argv[1].endswith(f"{R2['BACKUP_R2_ENDPOINT']}/{R2['BACKUP_R2_BUCKET']}/{dump.name}.hmac")
    assert all("--aws-sigv4" in line and "-K -" in line and f"-T {out}/" in line for line in argv)
    assert all("s3cr3t-example" not in line for line in argv)
    assert all(line == 'stdin: user = "AKIDEXAMPLE:s3cr3t-example"' for line in stdin)
    assert result.stdout.count("off-site copy written") == 2
    assert "off-site copies on" in result.stdout


def test_a_failed_upload_is_logged_and_the_local_backup_stands(tmp_path: Path, env: ProductSettings) -> None:
    fake(tmp_path / "bin", "curl", "cat >/dev/null\nexit 22\n")
    out, result = run_backup(tmp_path, env, **R2)
    assert result.stderr.count("off-site copy FAILED") == 2
    assert len(dumps(out, env)) == 1
    assert len(dumps(out, env, ".hmac")) == 1


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        ({"BACKUP_R2_SECRET_ACCESS_KEY": ""}, "secret is empty"),
        ({"BACKUP_R2_ENDPOINT": "http://storage.invalid"}, "https://"),
        ({"BACKUP_R2_SECRET_ACCESS_KEY": "s3cr3t\nurl = https://evil.invalid/"}, "unexpected characters"),
    ],
)
def test_a_misconfigured_r2_setting_turns_uploads_off_loudly(
    tmp_path: Path, env: ProductSettings, broken: dict, reason: str
) -> None:
    out, result = run_backup(tmp_path, env, **{**R2, **broken})
    assert "MISCONFIGURED" in result.stderr and reason in result.stderr
    assert "off-site copies off" in result.stdout
    assert not (tmp_path / "curl-calls").exists()
    assert len(dumps(out, env)) == 1


def test_uploads_are_pinned_to_https() -> None:
    assert "--proto '=https'" in BACKUP.read_text(encoding="utf-8")


def test_a_restore_says_which_erasures_it_undid(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """A restore brings back data someone asked to have deleted, and takes the
    record that they asked away with it, so it has to say so.

    The erasure record lives in the same database as the data it records the
    erasure of, so a dump from before a request restores both the data and a
    world in which the request never happened. The restore cannot repair
    that.
    """
    result, _ = restore(tmp_path, env, dumps(backup_dir, env)[0])
    assert result.returncode == 0, result.stderr

    warning = result.stderr
    assert "undone erasures" in warning, warning
    # The boundary VALUE, not just the label. Asserting the label passed on the
    # failure branch too, where the message says the window is unknown - so a
    # regression that always failed the query read as a working warning.
    assert FAKE_ERASURE_AT in warning, warning
    assert "Newest erasure request in the restored data:" in warning, warning
    # The two things a person can actually do, and the thing they cannot.
    assert "You CANNOT list them" in warning, warning
    assert "run the product's purge again" in warning, warning
    assert "privacy contact address" in warning, warning
    assert "will NOT work again" in warning, warning


def test_the_erasure_boundary_is_the_products_own_query(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """The timestamp has to come from the restored rows, through ERASURE_CHECK_SQL.

    A boundary taken from the dump's FILENAME would be the moment the backup
    ran, not the newest erasure it contains.
    """
    _, received = restore(tmp_path, env, dumps(backup_dir, env)[0])
    argv = Path(f"{received}.argv").read_text()
    assert ERASURE_SQL in argv, f"the configured erasure query did not reach the database:\n{argv}"


def test_a_product_without_an_erasure_query_gets_no_erasure_step(
    tmp_path: Path, product_settings: Callable[..., ProductSettings]
) -> None:
    """ERASURE_CHECK_SQL empty: the restore applies the dump and ends, with no query and no warning."""
    plain = product_settings(ERASURE_CHECK_SQL="")
    out, _ = run_backup(tmp_path, plain)
    result, received = restore(tmp_path, plain, dumps(out, plain)[0])
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP
    assert len(Path(f"{received}.argv").read_text().splitlines()) == 1, "only the psql that applies the dump"
    assert "undone erasures" not in result.stderr
    assert not plaintext_left(tmp_path, plain)


# docker compose warns about every unset ${...} in the compose file, and the
# restore loads no env file. So this is on stderr for a perfectly successful
# query, and the exact text that made "stderr is empty" look like a synonym
# for "the read worked".
COMPOSE_NOISE = (
    'WARN[0000] The "POSTGRES_PASSWORD" variable is not set. Defaulting to a blank string.\n'
    'level=warning msg="The \\"REDIS_PASSWORD\\" variable is not set."\n'
)


def test_compose_warnings_do_not_turn_a_good_read_into_an_unknown_window(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """Discriminating on whether stderr is empty reads compose's interpolation
    warnings as a failed query. Exit status is what separates them."""
    result, _ = restore(tmp_path, env, dumps(backup_dir, env)[0], erasure_err=COMPOSE_NOISE)
    assert result.returncode == 0, result.stderr
    assert FAKE_ERASURE_AT in result.stderr, result.stderr
    assert "window is unknown" not in result.stderr, result.stderr


def test_an_empty_table_is_not_reported_as_a_boundary(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """No row survives the restore, so nothing bounds what it undid. That is
    the widest case, and also what a deployment that never received a request
    looks like, so the message says it cannot tell them apart."""
    result, _ = restore(tmp_path, env, dumps(backup_dir, env)[0], erasure_out="", erasure_err=COMPOSE_NOISE)
    assert result.returncode == 0, result.stderr
    assert "NO erasure request survives" in result.stderr, result.stderr
    assert "Newest erasure request" not in result.stderr, result.stderr


def test_a_failed_read_quotes_psql_and_not_the_compose_warnings(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """ "The table is not in this dump" and "postgres is not up" want opposite
    responses, so the reason has to survive - and it arrives AFTER compose's
    noise, which is why the last unfiltered line is the one taken.
    """
    psql_error = 'ERROR:  relation "deletion_requests" does not exist'
    # A warning on BOTH sides of the error, so `tail -1` alone cannot pass
    # this: the last line is a warning, and only the filter reaches the error.
    result, _ = restore(
        tmp_path,
        env,
        dumps(backup_dir, env)[0],
        erasure_out="",
        erasure_err=COMPOSE_NOISE + psql_error + "\n" + COMPOSE_NOISE,
        erasure_code=1,
    )
    assert result.returncode == 0, result.stderr
    assert "window is unknown" in result.stderr, result.stderr
    assert psql_error in result.stderr, result.stderr
    assert "POSTGRES_PASSWORD" not in result.stderr, result.stderr
    assert "NO erasure request survives" not in result.stderr, result.stderr


def test_a_rehearsal_is_not_told_the_live_stack_owes_someone_an_erasure(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """An alarm that fires identically on every drill is one the operator
    learns to dismiss, so the stakes line is the live stack's (the project is
    COMPOSE_PROJECT), and a rehearsal is told plainly that nothing is owed."""
    dump = dumps(backup_dir, env)[0]
    live, _ = restore(tmp_path, env, dump)
    assert f"restoring into compose project: {env.values['COMPOSE_PROJECT']}" in live.stderr
    assert "this deployment is holding data that someone asked us to delete" in live.stderr

    rehearsal, _ = restore(tmp_path, env, dump, RESTORE_PROJECT="drill")
    assert "restoring into compose project: drill" in rehearsal.stderr
    assert "nothing is owed here" in rehearsal.stderr, rehearsal.stderr
    assert "holding data that someone asked us to delete" not in rehearsal.stderr


def test_the_plaintext_dump_is_gone_before_the_boundary_query(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """The decrypted file is the entire database in cleartext. It is removed
    as soon as psql has read it; the trap stays for the paths that never get
    there."""
    _, received = restore(tmp_path, env, dumps(backup_dir, env)[0])
    listing = Path(f"{received}.tmpfiles")
    left = [line for line in listing.read_text().splitlines() if line.strip()]
    assert all(line.endswith(".err") for line in left), (
        f"the decrypted dump was still on disk during the boundary query: {left}"
    )


def test_a_restore_that_cannot_print_its_warning_still_succeeds(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """Everything in step 4 happens after the point of no return. vm-autostart
    calls restore.sh as a bare command with RESTORE_OK still 0, so a non-zero
    exit fires the cleanup that removes the volume this restore just filled."""
    result, _ = restore(tmp_path, env, dumps(backup_dir, env)[0], break_notice=True)
    assert result.returncode == 0, (
        f"a successful restore exited {result.returncode} because its warning failed"
    )
    assert "complete" in result.stdout


def test_a_failing_cleanup_does_not_turn_a_good_restore_into_a_failed_one(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """A read-only or full filesystem makes the unlink fail, and both the
    explicit rm after psql and the EXIT trap are then non-zero commands under
    `set -eu` - and a failing EXIT trap sets the script's status as well."""
    result, _ = restore(tmp_path, env, dumps(backup_dir, env)[0], break_cleanup=True)
    assert result.returncode == 0, (
        f"exited {result.returncode} because cleanup failed, which costs the database"
    )
    assert "undone erasures" in result.stderr, "the warning stopped as well"


def test_a_restore_whose_stdout_is_gone_still_succeeds(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """The completion line is the script's only stdout write, and a full disk
    fails it as readily as it fails the log it is being written to."""
    result, _ = restore(tmp_path, env, dumps(backup_dir, env)[0], close_stdout=True)
    assert result.returncode == 0, f"exited {result.returncode} because it could not announce success"


def test_a_restore_without_product_settings_refuses_before_anything(
    tmp_path: Path, backup_dir: Path, env: ProductSettings
) -> None:
    """restore.sh takes every product setting from PRODUCT_SETTINGS; without it there is nothing to restore into."""
    run_env = {k: v for k, v in os.environ.items() if k != "PRODUCT_SETTINGS"}
    result = subprocess.run(
        ["bash", str(RESTORE), str(dumps(backup_dir, env)[0])],
        env={**run_env, "BACKUP_PASSPHRASE": PASSPHRASE},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "PRODUCT_SETTINGS" in result.stderr, result.stderr


def test_restore_runs_from_the_packaged_layout_with_its_shared_helper(
    tmp_path: Path, backup_dir: Path, env: ProductSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "installed" / "restore.sh"
    packaged.parent.mkdir()
    (packaged.parent / "backup").mkdir()
    shutil.copy(RESTORE, packaged)
    shutil.copy(CRYPTO, packaged.parent / "backup" / "crypto.sh")
    monkeypatch.setattr(sys.modules[__name__], "RESTORE", packaged)
    result, received = restore(tmp_path, env, dumps(backup_dir, env)[0])
    assert result.returncode == 0, result.stderr
    assert received.read_text() == FAKE_DUMP


def test_disposable_drill_packages_the_helper_and_both_major_images() -> None:
    drill = (LIBRARY_ROOT / "checks" / "backup-restore-drill.sh").read_text()
    assert '"$REPO/deploy/backup/crypto.sh"' in drill
    assert 'cp "$REPO/deploy/backup/Dockerfile"' in drill
    assert "COPY scripts/ /scripts/" in drill
    assert "run_major 16 'postgres:16.15-alpine@sha256:" in drill
    assert "run_major 17 'postgres:17.11-alpine@sha256:" in drill
    assert 'bash "$REPO/deploy/restore.sh" "$cipher"' in drill
    assert '"$source_counts" = "$restored_counts"' in drill
    assert "compgen -v COMPOSE_" in drill
    assert "export COMPOSE_DISABLE_ENV_FILE=1" in drill
    assert drill.count('COMPOSE_FILE="$WORK/compose.yml" COMPOSE_ENV_FILES=/dev/null') == 2
    assert "for kind in container network volume" in drill
    assert drill.count('RESTORE_PROJECT="$PROJECT" RESTORE_SERVICE=restored RESTORE_UNVERIFIED=0') == 2
    assert '"${PROJECT}_default" "${PROJECT}_backups"' in drill
    assert "--remove-orphans" not in drill
    parsed = subprocess.run(
        ["bash", "-n", str(LIBRARY_ROOT / "checks" / "backup-restore-drill.sh")],
        capture_output=True,
        check=False,
    )
    assert parsed.returncode == 0


def test_disposable_drill_drops_inherited_compose_selectors_before_docker(tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    fake(
        tmp_path / "bin",
        "docker",
        f'printf "%s|%s|%s|%s\\n" "${{COMPOSE_FILE-absent}}" "${{COMPOSE_ENV_FILES-absent}}" "${{COMPOSE_PROFILES-absent}}" "${{COMPOSE_DISABLE_ENV_FILE-absent}}" >>"{seen}"\n[ "$1" != info ]\n',
    )
    result = subprocess.run(
        [
            "bash",
            str(LIBRARY_ROOT / "checks" / "backup-restore-drill.sh"),
            str(tmp_path / "report"),
            str(tmp_path),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "DOCKER_HOST": "unix:///fixture/docker.sock",
            "COMPOSE_FILE": "refuse-production.yml",
            "COMPOSE_ENV_FILES": "refuse-production-config",
            "COMPOSE_PROFILES": "refuse-production-profile",
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert seen.read_text().splitlines() == ["absent|absent|absent|1"] * 2
    assert (tmp_path / "report").read_text().splitlines()[-1] == "FAIL: backup restore drill"
