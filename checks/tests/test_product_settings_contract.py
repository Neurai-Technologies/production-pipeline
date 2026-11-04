"""product.settings.example and the files in deploy/ name the same settings, in both directions.

A script that requires a key the example does not list fails on first use in
every product that copied the example; one that reads an optional key the
example does not list has a setting nobody can find; a key in the example
that nothing reads does nothing while looking as if it did.

Scanned: every deploy/**/*.sh, every Dockerfile under deploy/,
deploy/compose.example.yml, deploy/restore-drill/docker-compose.yml,
deploy/deployer/ci-gate.jq and deploy/vm/launchagent.plist.template.
Required means `${KEY:?...}` or `${KEY?...}`, which covers every `: "${KEY:?`
line; optional means `${KEY:-...}` or `${KEY-...}`; read means `$KEY` or
`${KEY` anywhere in a scanned file.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from conftest import LIBRARY_ROOT, example_keys

DEPLOY = LIBRARY_ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.rglob("*.sh"))
SCANNED = sorted(
    {
        *SCRIPTS,
        *DEPLOY.rglob("Dockerfile"),
        DEPLOY / "compose.example.yml",
        DEPLOY / "restore-drill" / "docker-compose.yml",
        DEPLOY / "deployer" / "ci-gate.jq",
        DEPLOY / "vm" / "launchagent.plist.template",
    }
)
REQUIRED = re.compile(r"\$\{([A-Z][A-Z0-9_]*):?\?")
OPTIONAL = re.compile(r"\$\{([A-Z][A-Z0-9_]*):?-")
# Read by a scanned file but deliberately not in product.settings, with the reason.
NOT_PRODUCT_KEYS = {
    # Secrets and operator settings: compose interpolates them from the
    # product's .env into `environment:` (compose.example.yml).
    "GITHUB_DEPLOY_TOKEN": "a secret: the operator's .env, never product.settings",
    "BACKUP_PASSPHRASE": "a secret: the operator's .env, never product.settings",
    "DB_BACKUP_PASSWORD": "a secret: the operator's .env, never product.settings",
    "BACKUP_R2_ENDPOINT": "the operator's .env, through compose environment:",
    "BACKUP_R2_BUCKET": "the operator's .env, through compose environment:",
    "BACKUP_R2_ACCESS_KEY_ID": "a secret: the operator's .env, through compose environment:",
    "BACKUP_R2_SECRET_ACCESS_KEY": "a secret: the operator's .env, through compose environment:",
    "DEPLOY_ALERT_URL": "a secret (bot token in the URL): the operator's .env",
    "DEPLOY_POLL_SECONDS": "the operator's .env, through compose environment:",
    "DEPLOY_SITE_GRACE_SECONDS": "the operator's .env, through compose environment:",
    "BACKUP_KEEP_DAYS": "the operator's .env, through compose environment:",
    "BACKUP_MAX_DAYS": "the operator's .env, through compose environment:",
    # Paths inside the containers, fixed by compose.example.yml's mounts;
    # overridable only so the scripts can run under a test.
    "BACKUP_ROOT": "container mount path, fixed by compose",
    "BACKUP_HEARTBEAT": "container mount path, fixed by compose",
    "BACKUP_LOCK_DIR": "container mount path, fixed by compose",
    "LOG_ARCHIVE_ROOT": "container mount path, fixed by compose",
    "LOG_ARCHIVE_STAGE": "container tmpfs path, fixed by compose",
    "DOCKER_LOGS": "container mount path, fixed by compose",
    "DEPLOY_ENV_DIR": "container mount path, fixed by compose",
    # Set per run by the operator, a script or the system (the example's
    # "Not keys" list).
    "PRODUCT_SETTINGS": "the path of product.settings itself, set per run",
    "RESTORE_SERVICE": "set per run (the drill names its own service)",
    "RESTORE_PROJECT": "set per run (the drill names its own project)",
    "RESTORE_UNVERIFIED": "set per run, by hand only",
    "RESTORE_REQUIRE_V2": "set per run, by vm-autostart.sh's restore call",
    "TMPDIR": "the system's",
    # A script's own variable, defaulted where a message prints it.
    "FAILED_STEP": "deploy-loop.sh's own variable",
    "USERS": "vm-autostart.sh's own variable",
}


def keys_in(pattern: re.Pattern[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in SCANNED:
        for key in pattern.findall(path.read_text(encoding="utf-8")):
            name = str(path.relative_to(LIBRARY_ROOT))
            if name not in found.setdefault(key, []):
                found[key].append(name)
    return found


def reads(path: Path, key: str) -> bool:
    return re.search(rf"\$\{{?{key}\b", path.read_text(encoding="utf-8")) is not None


def test_the_files_are_found() -> None:
    assert len(SCRIPTS) >= 8 and all(path.is_file() for path in SCANNED), [str(p) for p in SCANNED]
    assert keys_in(REQUIRED) and keys_in(OPTIONAL), "nothing scanned; the contract would hold vacuously"


def test_every_required_key_is_in_the_example() -> None:
    keys = set(example_keys())
    missing = {
        key: files
        for key, files in sorted(keys_in(REQUIRED).items())
        if key not in keys and key not in NOT_PRODUCT_KEYS
    }
    assert not missing, f"required but missing from product.settings.example: {missing}"


def test_every_optional_key_is_in_the_example() -> None:
    """An optional setting is still a setting: the example lists it, and its
    comment may say it is optional and what the default is."""
    keys = set(example_keys())
    missing = {
        key: files
        for key, files in sorted(keys_in(OPTIONAL).items())
        if key not in keys and key not in NOT_PRODUCT_KEYS
    }
    assert not missing, f"read with a default but missing from product.settings.example: {missing}"


def test_every_key_in_the_example_is_read() -> None:
    unread = [key for key in example_keys() if not any(reads(path, key) for path in SCANNED)]
    assert not unread, f"in product.settings.example but read by no scanned file: {unread}"


def test_the_example_holds_no_secret_and_assigns_each_key_once() -> None:
    keys = example_keys()
    leaked = [key for key in keys if key in NOT_PRODUCT_KEYS]
    assert not leaked, f"a secret or per-run key is in product.settings.example: {leaked}"
    assert len(keys) == len(set(keys)), sorted({key for key in keys if keys.count(key) > 1})


# The script each compose service runs, so a key it reads can be traced to the
# service that must pin it.
SERVICE_SCRIPTS = {
    "backup": DEPLOY / "backup" / "backup.sh",
    "logarchive": DEPLOY / "backup" / "logarchive.sh",
    "deployer": DEPLOY / "deployer" / "deploy-loop.sh",
}


def mount_target(volume: str) -> str:
    """The container path of a short-form compose volume: the last field, less :ro or :rw.
    The source may itself hold colons (`${BACKUP_DIR:-./backups}`)."""
    parts = volume.split(":")
    if parts[-1] in ("ro", "rw"):
        parts = parts[:-1]
    return parts[-1]


def test_every_path_fixed_by_compose_is_pinned_in_compose() -> None:
    """env_file loads product.settings into the container, so a path key
    compose does not set in `environment:` could be moved by a product. Each
    service that reads such a key pins it to the script's own default, and
    that default is one of the service's mount or tmpfs paths, inside one, or
    the folder its file mounts sit in (/host-env)."""
    fixed = [key for key, why in NOT_PRODUCT_KEYS.items() if "fixed by compose" in why]
    assert fixed, "no key is labelled fixed by compose; update this test"
    services = yaml.safe_load((DEPLOY / "compose.example.yml").read_text(encoding="utf-8"))["services"]
    pinned_somewhere = set()
    for name, script in SERVICE_SCRIPTS.items():
        text = script.read_text(encoding="utf-8")
        service = services[name]
        targets = [mount_target(str(v)) for v in service.get("volumes", [])] + list(service.get("tmpfs", []))
        for key in fixed:
            defaults = re.findall(rf"\$\{{{key}:-([^}}]*)\}}", text)
            if not defaults:
                continue
            assert len(defaults) == 1, f"{script.name} defaults {key} more than once: {defaults}"
            value = service.get("environment", {}).get(key)
            assert value == defaults[0], (
                f"{name}: {key} is {value!r} in compose, {defaults[0]!r} in {script.name}"
            )
            assert any(
                value == t or value.startswith(t + "/") or t.startswith(value + "/") for t in targets
            ), f"{name}: {key}={value} matches none of its mounts {targets}"
            pinned_somewhere.add(key)
    assert pinned_somewhere == set(fixed), (
        f"labelled fixed by compose but read by no service: {set(fixed) - pinned_somewhere}"
    )
