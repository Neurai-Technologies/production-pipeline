set shell := ["bash", "-euo", "pipefail", "-c"]

ci: docs paths bypasses secrets checks

# One-time per clone, from a normal shell: hook shims, hooks path, tool warnings.
# The hooks run on terminal commits and pushes; git-gate runs none.
setup:
    bash scripts/setup-hooks.sh

docs:
    python3 scripts/check_docs.py

# No tracked path may match the sensitive-path list (the hook also checks each commit
# in a clone where `just setup` ran).
paths:
    git ls-files -z | bash scripts/sensitive-paths.sh

# The scan is only as strong as its config: no ignore file (every scan already
# ignores inline allow comments).
bypasses:
    test ! -e .gitleaksignore

# Committed history only. `gitleaks dir .` would also read gitignored files
# (a local .env, a virtualenv), so every push from a clone that keeps real
# local secrets would fail. Each staged change is scanned by the pre-commit
# hook in a clone where `just setup` ran, and by git-gate for a session; GitHub
# CI scans the files of its clean checkout.
secrets:
    if git rev-parse --verify HEAD >/dev/null 2>&1; then gitleaks git . --redact --no-banner --ignore-gitleaks-allow --config .gitleaks.toml; else echo "No commits yet; nothing to scan (the pre-commit hook scans each staged change)."; fi

# The library's check suite (pytest 9.1.1 and PyYAML 6.0.3; see docs/VERIFICATION.md).
checks:
    cd checks && python3 -m pytest -q

# Copy the shared block from CLAUDE.md into the other agent files.
sync-agents:
    python3 scripts/sync_agents.py

# Runs from the pre-commit hook when defined, never through git-gate. Keep it
# fast; the pre-push hook and GitHub CI run ci.
pre-commit: docs bypasses
