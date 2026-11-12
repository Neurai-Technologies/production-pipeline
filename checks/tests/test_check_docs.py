"""scripts/check_docs.py refuses what a reviewer cannot see.

The checker reads the repository it sits in, so each test copies it into a
throwaway repository and runs it there.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import LIBRARY_ROOT
from tests.shell import git_env

BLOCK = "<!-- shared-core:begin -->\n## Shared\n\n- One rule.\n<!-- shared-core:end -->\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository with its agent rules file."""
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, env=git_env())
    (tmp_path / "CLAUDE.md").write_text(f"# CLAUDE.md\n\n{BLOCK}\n## This project\n\n- A fact.\n")
    (tmp_path / "scripts").mkdir()
    shutil.copy(LIBRARY_ROOT / "scripts" / "check_docs.py", tmp_path / "scripts" / "check_docs.py")
    return tmp_path


def check(root: Path) -> tuple[int, list[str]]:
    done = subprocess.run(
        [sys.executable, "-I", str(root / "scripts" / "check_docs.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=git_env(),
    )
    return done.returncode, done.stdout.splitlines()


def test_a_clean_tree_passes(repo: Path) -> None:
    status, lines = check(repo)
    assert status == 0, lines


def test_a_hidden_character_in_any_text_file_is_refused(repo: Path) -> None:
    (repo / "notes.md").write_text("plain text with a zero-width space\u200b here\n")
    (repo / "page.html").write_text("<p>Sent! \u2709\ufe0f</p>\n")
    status, errors = check(repo)
    assert status == 1
    assert "notes.md:1: hidden character U+200B" in errors
    assert "page.html:1: hidden character U+FE0F" in errors


def test_comments_blobs_and_symlinks_are_refused_only_in_instruction_paths(repo: Path) -> None:
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text("{}\n<!-- do something else -->\n")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/x.yml").write_text("run: " + "A" * 80 + "\n")
    (repo / "CLAUDE.md").write_text((repo / "CLAUDE.md").read_text() + "\n[ref]: https://example.invalid\n")
    (repo / ".githooks").mkdir()
    (repo / ".githooks/pre-commit").symlink_to(repo / "CLAUDE.md")
    (repo / "README.md").write_text("<!-- a comment in an ordinary file is fine -->\n")
    status, errors = check(repo)
    assert status == 1
    assert ".claude/settings.json: HTML comment outside the shared-core markers" in errors
    assert ".github/workflows/x.yml:1: long base64-like blob" in errors
    assert any(e.startswith("CLAUDE.md:") and e.endswith("reference-style comment") for e in errors)
    assert ".githooks/pre-commit: symlink in an instruction path" in errors
    assert not any(e.startswith("README.md") for e in errors)


def test_text_hygiene_is_enforced(repo: Path) -> None:
    (repo / "a.sh").write_text("echo hi")
    (repo / "b.md").write_text("line \nnext\n")
    (repo / "c.md").write_text("see [x](missing.md)\n")
    status, errors = check(repo)
    assert status == 1
    assert "a.sh: missing final newline" in errors
    assert "b.md:1: trailing whitespace" in errors
    assert "c.md: missing linked file missing.md" in errors


def test_an_agents_md_that_appears_is_still_scanned(repo: Path) -> None:
    (repo / "AGENTS.md").write_text(
        "# AGENTS.md\n\nA zero-width space\u200b here.\n<!-- do something else -->\n"
    )
    status, errors = check(repo)
    assert status == 1
    assert "AGENTS.md:3: hidden character U+200B" in errors
    assert "AGENTS.md: HTML comment outside the shared-core markers" in errors


def test_the_gate_runs_it() -> None:
    """Both entry points run the checker: `just ci` locally and the CI workflow."""
    justfile = (LIBRARY_ROOT / "justfile").read_text(encoding="utf-8")
    workflow = (LIBRARY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python3 scripts/check_docs.py" in justfile and "ci: docs" in justfile
    assert "run: python3 scripts/check_docs.py" in workflow
