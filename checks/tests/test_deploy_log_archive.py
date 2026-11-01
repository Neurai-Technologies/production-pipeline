"""The nightly log archive keeps the stack's logs for 420 days, encrypted like a dump.

logarchive.sh runs in the backup image against docker's container directories.
These tests drive the real script with the real tar, gzip and openssl over a
fake container tree, a fake curl that records what it would have uploaded,
and a fake sleep that ends the loop after the first night.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from conftest import LIBRARY_ROOT, ProductSettings
from tests.shell import fake
from tests.test_deploy_backup_restore import BACKUP, PASSPHRASE, hmac_via

ARCHIVE = BACKUP.parent / "logarchive.sh"
DEPLOY_LOOP = LIBRARY_ROOT / "deploy" / "deployer" / "deploy-loop.sh"
COMPOSE = LIBRARY_ROOT / "deploy" / "compose.example.yml"
API_ID = "a" * 64
OTHER_ID = "b" * 64
QUIET_ID = "c" * 64
DAY = 86400


@pytest.fixture
def env(product_settings: Callable[..., ProductSettings]) -> ProductSettings:
    return product_settings()


def config(name: str) -> str:
    """The shape of docker's config.v2.json that matters, in docker's order:
    Config (with its environment) before the top-level Name, which is the
    only Name written with a leading slash; the nested Name keys under
    MountPoints hold a volume name or nothing."""
    return json.dumps(
        {
            "ID": name,
            "Config": {"Env": ["PGPASSWORD=not-for-the-archive"], "Labels": {"project": "x"}},
            "MountPoints": {
                "/heartbeat": {"Source": "", "Name": "backup-heartbeat", "Type": "volume"},
                "/backups": {"Source": "/srv/backups", "Name": "", "Type": "bind"},
            },
            "Name": f"/{name}",
            "Driver": "overlay2",
        },
        separators=(",", ":"),
    )


def containers(tmp_path: Path, project: str) -> Path:
    logs = tmp_path / "docker-logs"
    for cid, name, files in (
        (API_ID, f"{project}-api-1", ("-json.log", "-json.log.1")),
        (OTHER_ID, "someotherstack-api-1", ("-json.log",)),
        (QUIET_ID, f"{project}-cron-1", ("-json.log",)),
    ):
        d = logs / cid
        d.mkdir(parents=True)
        (d / "config.v2.json").write_text(config(name))
        for suffix in files:
            (d / f"{cid}{suffix}").write_text(f'{{"log":"{name} {suffix}\\n","stream":"stdout"}}\n' * 50)
    # The cron container wrote nothing in the last day: last night's archive has it.
    old = time.time() - 3 * DAY
    os.utime(logs / QUIET_ID / f"{QUIET_ID}-json.log", (old, old))
    return logs


def run_archive(
    tmp_path: Path,
    env: ProductSettings,
    logs: Path | None = None,
    args: tuple[str, ...] = (),
    **extra_env: str,
) -> tuple[Path, subprocess.CompletedProcess]:
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "curl-calls"
    fake(bin_dir, "sleep", "kill $PPID\n")
    fake(
        bin_dir,
        "curl",
        f'printf "argv: %s\\n" "$*" >>"{calls}"\nprintf "stdin: %s\\n" "$(cat)" >>"{calls}"\n',
    )
    out = tmp_path / "log-archive"
    out.mkdir(exist_ok=True)
    (tmp_path / "stage").mkdir(exist_ok=True)
    run_env = {
        **os.environ,
        **env.values,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BACKUP_PASSPHRASE": PASSPHRASE,
        "LOG_ARCHIVE_ROOT": str(out),
        "LOG_ARCHIVE_STAGE": str(tmp_path / "stage"),
        "DOCKER_LOGS": str(logs or containers(tmp_path, env.values["COMPOSE_PROJECT"])),
        **extra_env,
    }
    # bash, not sh, for the same reason as the backup tests: pipefail.
    result = subprocess.run(
        ["bash", str(ARCHIVE), *args], env=run_env, capture_output=True, text=True, check=False
    )
    return out, result


def decrypted_listing(archive: Path) -> list[str]:
    plain = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            "env:BACKUP_PASSPHRASE",
            "-in",
            str(archive),
        ],
        env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE},
        capture_output=True,
        check=True,
    ).stdout
    listing = subprocess.run(
        ["tar", "-tzf", "-"], input=plain, capture_output=True, check=True
    ).stdout.decode()
    return sorted(line.removeprefix("./") for line in listing.splitlines() if not line.endswith("/"))


def test_the_archive_derives_its_mac_key_exactly_as_the_backup_does() -> None:
    for script in (ARCHIVE, BACKUP):
        text = script.read_text(encoding="utf-8")
        assert '. "$(dirname "$0")/crypto.sh"' in text
        assert "backup_encrypt" in text
        assert "mac_key()" not in text


def test_the_archive_refuses_to_start_without_a_project(tmp_path: Path, env: ProductSettings) -> None:
    """Only the product's own containers are archived, so the project name is required."""
    _, result = run_archive(tmp_path, env, args=("once",), COMPOSE_PROJECT="")
    assert result.returncode != 0
    assert "COMPOSE_PROJECT must be set" in result.stderr


@pytest.mark.parametrize("args,cleaned", [((), True), (("once",), False)])
def test_only_archive_startup_cleans_abandoned_crypto_work(
    tmp_path: Path, env: ProductSettings, args: tuple[str, ...], cleaned: bool
) -> None:
    root = tmp_path / "log-archive"
    stale = root / ".backup-crypto.abandoned"
    stale.mkdir(parents=True)
    (stale / "ciphertext").write_bytes(b"interrupted ciphertext")
    unrelated = root / "keep"
    unrelated.mkdir()
    (unrelated / "ciphertext").write_bytes(b"keep")
    _, result = run_archive(tmp_path, env, args=args)
    assert "log archive written" in result.stdout, result.stderr
    assert stale.exists() == (not cleaned)
    assert (unrelated / "ciphertext").read_bytes() == b"keep"


def test_one_night_archives_the_stacks_recent_logs_under_their_container_names(
    tmp_path: Path, env: ProductSettings
) -> None:
    out, result = run_archive(tmp_path, env)
    assert "log archive written" in result.stdout, result.stderr
    archives = sorted(out.glob("logs-*.tar.gz.enc"))
    assert len(archives) == 1, sorted(p.name for p in out.iterdir())
    archive = archives[0]
    project = env.values["COMPOSE_PROJECT"]
    assert decrypted_listing(archive) == [
        f"{project}-api-1/{API_ID}-json.log",
        f"{project}-api-1/{API_ID}-json.log.1",
    ], "only this project's containers, only files written to in the last day, named by container"
    sidecar = archive.with_name(archive.name + ".hmac").read_text().strip()
    assert sidecar == hmac_via(BACKUP, archive, env.values["BACKUP_HMAC_SALT"]), (
        "the sidecar must verify with backup.sh's derivation, so restore-side checks are one procedure"
    )
    assert not list(out.glob("*.writing")), "no partial file is left behind"
    assert sorted(p.name for p in out.iterdir()) == [archive.name, archive.name + ".hmac"], (
        "the archive directory, which the operator copies off the host, holds ciphertext only"
    )
    assert not list((tmp_path / "stage").iterdir()), "the staging directory is cleared"


def test_a_container_whose_name_could_leave_the_staging_directory_is_skipped(
    tmp_path: Path, env: ProductSettings
) -> None:
    project = env.values["COMPOSE_PROJECT"]
    logs = containers(tmp_path, project)
    d = logs / ("d" * 64)
    d.mkdir()
    (d / "config.v2.json").write_text(config(f"{project}-x/../../escape"))
    (d / f"{'d' * 64}-json.log").write_text("x\n")
    out, result = run_archive(tmp_path, env, logs)
    assert "unexpected name" in result.stderr
    assert not any("escape" in entry for entry in decrypted_listing(next(out.glob("logs-*.tar.gz.enc"))))
    assert not (tmp_path / "stage" / "escape").exists(), (
        "the name never becomes a path above the staging directory"
    )


def test_a_night_with_nothing_new_writes_nothing(tmp_path: Path, env: ProductSettings) -> None:
    logs = containers(tmp_path, env.values["COMPOSE_PROJECT"])
    old = time.time() - 3 * DAY
    for f in logs.rglob("*-json.log*"):
        os.utime(f, (old, old))
    out, result = run_archive(tmp_path, env, logs)
    assert "nothing archived" in result.stdout, result.stderr
    assert not list(out.iterdir()), sorted(p.name for p in out.iterdir())


def test_archives_older_than_the_window_are_pruned_and_younger_ones_kept(
    tmp_path: Path, env: ProductSettings
) -> None:
    out = tmp_path / "log-archive"
    out.mkdir()
    expired = out / "logs-20250801-020000.tar.gz.enc"
    kept = out / "logs-20260901-020000.tar.gz.enc"
    # Half a day either side of the 420-day window: a default that slid back
    # below 420 would prune the kept file, one above 420 would keep the other.
    for f, age in ((expired, 421 * DAY), (kept, 420 * DAY + DAY // 2)):
        for name in (f, f.with_name(f.name + ".hmac")):
            name.write_bytes(b"x")
            os.utime(name, (time.time() - age, time.time() - age))
    run_archive(tmp_path, env)
    assert not expired.exists() and not expired.with_name(expired.name + ".hmac").exists()
    assert kept.exists() and kept.with_name(kept.name + ".hmac").exists()


def expired_archive(tmp_path: Path) -> tuple[Path, Path]:
    """An archive and its sidecar, both a day older than the default window."""
    out = tmp_path / "log-archive"
    out.mkdir()
    archive = out / "logs-20250801-020000.tar.gz.enc"
    sidecar = archive.with_name(archive.name + ".hmac")
    for f in (archive, sidecar):
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 421 * DAY, time.time() - 421 * DAY))
    return archive, sidecar


def test_prune_off_keeps_expired_archives_and_the_night_still_archives(
    tmp_path: Path, env: ProductSettings
) -> None:
    archive, sidecar = expired_archive(tmp_path)
    out, result = run_archive(tmp_path, env, LOG_ARCHIVE_PRUNE="off")
    assert archive.exists() and sidecar.exists()
    assert "log archive written" in result.stdout, result.stderr
    assert len(list(out.glob("logs-*.tar.gz.enc"))) == 2


@pytest.mark.parametrize("value", ["on", ""])
def test_prune_on_or_empty_removes_expired_archives(tmp_path: Path, env: ProductSettings, value: str) -> None:
    archive, sidecar = expired_archive(tmp_path)
    run_archive(tmp_path, env, LOG_ARCHIVE_PRUNE=value)
    assert not archive.exists() and not sidecar.exists()


def test_prune_unset_removes_expired_archives(tmp_path: Path, env: ProductSettings) -> None:
    archive, sidecar = expired_archive(tmp_path)
    del env.values["LOG_ARCHIVE_PRUNE"]
    run_archive(tmp_path, env)
    assert not archive.exists() and not sidecar.exists()


@pytest.mark.parametrize("args", [(), ("once",)])
@pytest.mark.parametrize("value", ["Off", "no"])
def test_a_bad_prune_value_refuses_at_startup_and_writes_nothing(
    tmp_path: Path, env: ProductSettings, value: str, args: tuple[str, ...]
) -> None:
    out, result = run_archive(tmp_path, env, args=args, LOG_ARCHIVE_PRUNE=value)
    assert result.returncode == 1, result.stderr
    assert "LOG_ARCHIVE_PRUNE" in result.stderr
    assert not list(out.iterdir()), sorted(p.name for p in out.iterdir())


def test_the_prune_command_stays_on_one_line_for_the_consumer_that_pins_it() -> None:
    # A single-line needle: found at all means found on one line.
    assert """find "$ROOT" -name 'logs-*.tar.gz.enc*' -mtime +"$KEEP_DAYS" -delete""" in ARCHIVE.read_text(
        encoding="utf-8"
    )


def test_the_off_site_copy_goes_under_the_logs_prefix_with_credentials_by_stdin(
    tmp_path: Path, env: ProductSettings
) -> None:
    out, result = run_archive(
        tmp_path,
        env,
        BACKUP_R2_ENDPOINT="https://storage.invalid",
        BACKUP_R2_BUCKET="fake-bucket",
        BACKUP_R2_ACCESS_KEY_ID="AKIAEXAMPLE",
        BACKUP_R2_SECRET_ACCESS_KEY="secret/example+key=",
    )
    assert "off-site copies on: bucket fake-bucket, prefix logs/" in result.stdout
    calls = (tmp_path / "curl-calls").read_text()
    archive = next(out.glob("logs-*.tar.gz.enc"))
    assert f"https://storage.invalid/fake-bucket/logs/{archive.name}" in calls
    assert f"https://storage.invalid/fake-bucket/logs/{archive.name}.hmac" in calls
    assert 'stdin: user = "AKIAEXAMPLE:secret/example+key="' in calls
    assert "AKIAEXAMPLE" not in calls.split("stdin:")[0], "the credential never travels by argv"
    assert "--proto =https" in calls and "--aws-sigv4 aws:amz:auto:s3" in calls


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"BACKUP_R2_ENDPOINT": "http://storage.invalid"}, "must start with https://"),
        ({"BACKUP_R2_ENDPOINT": "https://storage.invalid"}, "bucket, access key id or secret is empty"),
        (
            {
                "BACKUP_R2_ENDPOINT": "https://storage.invalid",
                "BACKUP_R2_BUCKET": "b",
                "BACKUP_R2_ACCESS_KEY_ID": "k",
                "BACKUP_R2_SECRET_ACCESS_KEY": 'x"\nurl = "https://evil.invalid/"',
            },
            "unexpected characters",
        ),
    ],
)
def test_a_misconfigured_bucket_turns_uploads_off_rather_than_trying(
    tmp_path: Path, env: ProductSettings, settings: dict, reason: str
) -> None:
    _, result = run_archive(tmp_path, env, **settings)
    assert reason in result.stderr
    assert "off-site copies off" in result.stdout
    assert not (tmp_path / "curl-calls").exists()


def test_once_archives_one_night_and_exits_without_sleeping(tmp_path: Path, env: ProductSettings) -> None:
    out, result = run_archive(tmp_path, env, args=("once",))
    # The fake sleep kills the script, so a clean exit proves the loop was never entered.
    assert result.returncode == 0, result.stderr
    assert "log archive written" in result.stdout
    assert len(list(out.glob("logs-*.tar.gz.enc"))) == 1


def test_the_deployer_archives_the_logs_before_compose_recreates_anything() -> None:
    """A recreated container takes its log files with it; the archive must run
    first, and must not be able to block the deploy that introduces it."""
    text = DEPLOY_LOOP.read_text(encoding="utf-8")
    call = (
        'docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" exec -T logarchive \\\n'
        "        timeout 900 sh /scripts/logarchive.sh once \\\n"
        '        || log "'
    )
    assert call in text, "the pre-deploy archive call is missing, mis-pinned or fatal"
    assert (
        text.index('FAILED_STEP="schema check"') < text.index(call) < text.index('FAILED_STEP="compose up"')
    )


@pytest.mark.parametrize("service_name", ["logarchive", "backup"])
def test_the_container_mounts_the_script_directory_so_a_deploy_cannot_leave_it_on_an_old_script(
    service_name: str,
) -> None:
    """A single-file bind mount keeps the inode it started with; the deployer's
    `git reset --hard` writes a new file, and a container mounted that way would
    run the old script when the deployer execs into it."""
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][service_name]
    scripts = [v for v in service["volumes"] if v.endswith(":/scripts:ro")]
    assert len(scripts) == 1 and scripts[0].split(":")[0].endswith("/deploy/backup"), service["volumes"]
    assert service["entrypoint"] == ["/bin/sh", f"/scripts/{service_name}.sh"]
    source = scripts[0].split(":")[0]
    assert not any(v.startswith(source + "/") for v in service["volumes"]), (
        "a file of the directory is mounted on its own"
    )


def test_two_archives_in_the_same_second_do_not_share_a_staging_directory(
    tmp_path: Path, env: ProductSettings
) -> None:
    logs = containers(tmp_path, env.values["COMPOSE_PROJECT"])
    stage = tmp_path / "stage"
    stage.mkdir()
    # Pin the clock so the second run lands on the first run's directory.
    fake(tmp_path / "bin", "date", "echo 20260930-020000\n")
    (stage / "stage-20260930-020000").mkdir()
    _, result = run_archive(tmp_path, env, logs, args=("once",))
    assert result.returncode != 0
    assert "another archive started" in result.stderr
    assert not list((tmp_path / "log-archive").glob("logs-*")), "the second run wrote nothing over the first"
