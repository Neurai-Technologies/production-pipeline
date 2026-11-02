# Project brief

- Name and purpose: `production-pipeline`, the shared, versioned CI/CD and ops
  library for Neurai Technologies Private Limited products.
- Users: the product repositories, which call its workflows or pull it by
  git subtree, pinning a release by its full commit SHA (see the
  2026-10-23 SHA-pin row in [DECISIONS.md](DECISIONS.md)).
- Scope: reusable GitHub workflows (ci, promote, worker-deploy), the VM
  deploy loop, backup and log-archive images, restore and restore-drill
  scripts, the macOS LaunchAgent VM autostart script and its installer,
  generic check tests. Non-goals: site tooling; anything product-specific.
- Stack: Bash, Python 3.11+ standard library, YAML (GitHub Actions),
  container images. Tool versions are recorded in
  [VERIFICATION.md](VERIFICATION.md) as code lands.
- Data: none collected or stored. Backups the code makes belong to the
  product that runs it.
- Branch and deployment policy: reviewed work is pushed to `ci`; CI runs; a
  green run promotes `main` by fast-forward
  (`.github/workflows/promote.yml`, with `GITHUB_TOKEN`; no other token,
  environment or secret). The owner pushes `ci` and creates tags from a
  terminal, because sessions cannot write to this organisation's
  repositories. If GitHub refuses a promotion, for example one that changes
  a workflow file, or the promote gate stays blocked, the owner checks that
  this repository's own runs on that sha are green, then pushes the same sha
  to `main` by hand.
  `git-gate` is the only route for session git writes. `lint`,
  `code-reviewer` and `security-engineer` run once per batch over the range.
  No deploy-on-push. A release is an immutable tag `vX.Y.Z` on `main`. Release
  owner: the Neurai Technologies Private Limited repository administrator.
- Licensing and visibility: proprietary ([LICENSE](../LICENSE)); public
  repository.
- Open, once the repository is on GitHub:
  - apply both rulesets once:
    `gh api -X POST repos/Neurai-Technologies/production-pipeline/rulesets --input .github/rulesets/main.json`
    and the same with `.github/rulesets/tags.json`;
  - Actions settings: "Require approval for all outside collaborators",
    and default workflow permissions read-only;
  - confirm GitHub Pages is off.

Adopting and updating the library in a product: [ADOPT.md](ADOPT.md).

Use [DECISIONS.md](DECISIONS.md) for rationale and
[DEPENDENCIES.md](DEPENDENCIES.md) for dependency records.
