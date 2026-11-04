"""Verification cannot be turned green by a moving action tag or a sensitive path.

Two generic guards from the product's verification suite: every workflow
action is pinned to a commit with its version recorded, and the one
sensitive-path list (scripts/sensitive-paths.sh, used by the hook, `just ci`
and CI) blocks every secret shape while letting templates and this tree
through.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from conftest import LIBRARY_ROOT
from tests.shell import git_env

GATE = LIBRARY_ROOT / "scripts" / "sensitive-paths.sh"


def test_every_action_is_pinned_to_a_commit_with_its_version_recorded() -> None:
    """A major tag is a moving pointer somebody else controls. Pinning to a
    commit makes an upgrade a deliberate, readable diff; the trailing comment
    says which release the sha was, so nobody has to resolve it to know.
    Comment lines (a caller's usage example) are not steps."""
    steps = []
    for workflow in sorted((LIBRARY_ROOT / ".github" / "workflows").glob("*.yml")):
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if match := re.search(r"uses:\s*(\S+)(.*)$", line):
                steps.append((workflow.name, number, match.group(1), match.group(2)))
    assert steps, "no workflow steps found; this guard would pass vacuously"
    for name, number, reference, rest in steps:
        action, _, version = reference.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", version), f"{name}:{number} {action} is not pinned to a commit"
        assert re.search(r"#\s*v\d+\.\d+", rest), f"{name}:{number} {action} records no version"


def gate(paths: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GATE)], input="\0".join(paths).encode(), capture_output=True, check=False
    )


@pytest.mark.parametrize(
    "path",
    [
        "deploy/app.env",
        ".env",
        "site/.env.local",
        ".envrc",
        "site/.dev.vars",
        "private.pem",
        "nested/id_ed25519",
        "api-token",
        "upload.jks",
        "release.keystore",
        ".env-local",
        "config/env.json",
        "AuthKey_ABC123.p8",
        "infra/prod.tfstate.backup",
        "client_secret_1234.json",
        "deploy/credentials",
        "deploy/secrets/anything.txt",
        "backups/2026-09-30.tar",
        "keystores/upload.txt",
        "local.properties",
        "dev.sqlite3",
        "dump.sql.gz.enc",
        "data.db-wal",
        "Nested/Secrets.ENC",
        "x.example/id_rsa",
    ],
)
def test_sensitive_paths_block_without_reading_contents(path: str) -> None:
    done = gate([path])
    assert done.returncode == 1, f"{path} passed the path gate"
    assert b"sensitive path" in done.stderr


@pytest.mark.parametrize(
    "path",
    [
        "deploy/product.settings.example",
        "deploy/.env.sample",
        "keys/id_rsa.template",
        "config/credentials.template",
        "src/backups/rotate.py",
        "app/credentials/__init__.py",
        "docs/token-lifetimes.md",
        "deploy/launchagent.plist.template",
    ],
)
def test_templates_and_source_folders_pass_the_path_gate(path: str) -> None:
    """Templates pass (the suffix must end the whole path: a directory called
    x.example/ gives no cover), and backups/ or credentials/ block only at the
    repository root."""
    done = gate([path])
    assert done.returncode == 0, done.stderr.decode()


def test_the_path_gate_lets_this_tree_commit() -> None:
    """`just ci` and CI run it over every tracked path, so a rule that matches
    one of them fails every run until someone deletes the rule. New, not-ignored
    files count: they are the next commit. The pre-commit hook checks only staged
    paths, in a clone where `just setup` ran; git-gate never runs it."""
    names = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=LIBRARY_ROOT,
        capture_output=True,
        check=True,
        env=git_env(),
    ).stdout
    done = subprocess.run(["bash", str(GATE)], input=names, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode()
