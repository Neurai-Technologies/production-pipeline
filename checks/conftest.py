"""Fixtures shared by every check: where the library is, and a fake product.settings."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

LIBRARY_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = LIBRARY_ROOT / "deploy" / "product.settings.example"
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# Values whose shape a test depends on. Every other key of the example gets
# fake-<key>, so no value can be mistaken for a real product's. Paths are
# placed under the test's own tmp_path.
FAKE = {
    "PRODUCT": "fakeproduct",
    "COMPOSE_PROJECT": "fakestack",
    "COMPOSE_DIR": "deploy",
    "LIBRARY_DIR": "lib",
    "DB_SERVICE": "fakedb",
    "DB_USER": "fakeowner",
    "DB_NAME": "fakedata",
    "BACKUP_PREFIX": "fakedump",
    # 8 bytes: openssl -S keeps no more (test_every_byte_of_the_salt_keys_the_mac).
    "BACKUP_HMAC_SALT": "fakesalt",
    "GITHUB_REPO": "fake-org/fake-repo",
    "DEPLOY_BRANCH": "main",
    "SOURCE_BRANCH": "ci",
    "TESTS_WORKFLOW": ".github/workflows/fake-tests.yml",
    "PROMOTE_WORKFLOW": ".github/workflows/fake-promote.yml",
    "ENV_FILES": ".env app.env",
    "APP_IGNORE_PATHS": "site docs *.md",
    "APP_PATHS": "",
    "SITE_URL": "https://site.invalid/_release.json",
    "DB_VOLUME": "fakevolume",
    "MIGRATE_SERVICE": "fakemigrate",
    "MIGRATE_CHECK_CMD": "fake-migrate --check",
    "API_SERVICE": "fakeapi",
    "API_READY_CMD": "fake-api --ready",
    "WORKER_SERVICE": "fakeworker",
    "WORKER_READY_CMD": "fake-worker --ready",
    "PRE_DEPLOY_HOOK": "",
    "POST_DEPLOY_HOOK": "",
    "SITE_RELEASE_HOOK": "",
    "DB_BACKUP_USER": "fakereader",
    "COLIMA_PROFILE": "fakevm",
    "LAUNCH_LABEL": "invalid.fake.vm-autostart",
    "ORIGIN_URL": "https://git.invalid/fake-org/fake-repo.git",
    "RESTORE_VERIFY_SQL": "select count(*) from fake_accounts",
    "ERASURE_CHECK_SQL": "",
    "DATA_DISK_DEVICE": "/dev/fakedisk1",
    "MAX_DUMP_AGE_HOURS": "48",
    "DEPLOYER_SERVICE": "fakedeployer",
    "OPS_PROFILE": "fakeops",
    # The archive's own project is the stack's unless a product says otherwise.
    "LOG_ARCHIVE_PROJECT": "fakestack",
    "LOG_ARCHIVE_PRUNE": "",
    "POSTGRES_INIT_DIR": "deploy/fake-init",
    "BACKUP_MIRROR_EXTRA_SOURCES": "",
}
PATHS = {
    "SRC_DIR": "src",
    "REPO_DIR": "checkout",
    "COLIMA_HOME": "vm-home",
    "APP_SUPPORT_DIR": "support",
    "BACKUP_DIR": "backups",
}


@dataclass(frozen=True)
class ProductSettings:
    """A product.settings written for one test: its path, and the values in it."""

    path: Path
    values: dict[str, str]


def example_keys() -> list[str]:
    """Every key deploy/product.settings.example assigns, in file order."""
    return ASSIGNMENT.findall(EXAMPLE.read_text(encoding="utf-8"))


def numeric_example_keys() -> dict[str, str]:
    """Keys whose example value is a plain integer, with that value. A number
    identifies nothing, and a script that does arithmetic or find -mtime on
    the value needs a number, so the fixture keeps it."""
    found = {}
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        value = value.strip().strip("\"'")
        if sep and ASSIGNMENT.match(line) and value.isdigit():
            found[key] = value
    return found


@pytest.fixture(autouse=True)
def _no_inherited_git_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every inherited GIT_* variable for each test.

    A git hook exports GIT_DIR; without this, any git a test or a script
    under test runs would act on the repository the hook runs in.
    """
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def library_root() -> Path:
    """The root of the production-pipeline checkout these checks belong to."""
    return LIBRARY_ROOT


@pytest.fixture
def product_settings(tmp_path: Path) -> Callable[..., ProductSettings]:
    """A factory writing tmp_path/product.settings with every key of the example.

    Values are obviously fake; keyword arguments override or add keys. The
    file is shell-quoted, so a script that sources it reads each value back
    exactly.
    """

    def write(**overrides: str) -> ProductSettings:
        values = {}
        numbers = numeric_example_keys()
        for key in example_keys():
            if key in PATHS:
                values[key] = str(tmp_path / PATHS[key])
            elif key in FAKE:
                values[key] = FAKE[key]
            elif key in numbers:
                values[key] = numbers[key]
            else:
                values[key] = f"fake-{key.lower().replace('_', '-')}"
        values.update(overrides)
        path = tmp_path / "product.settings"
        path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
        return ProductSettings(path=path, values=values)

    return write
