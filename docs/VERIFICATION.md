# Verification

## Multiline settings parsing

- 2026-10-30, the unchanged settings format from cde8a37: Docker Compose 5.1.3
  `config --format json` loaded a synthetic `product.settings` via a fixture
  service's `env_file`. A double-quoted `BACKUP_MIRROR_EXTRA_SOURCES` containing
  two records, separated by one literal newline, retained both that newline
  and spaces in the source paths. The parsed service environment matched the
  expected string exactly (one equality assertion passed). The command used
  a new empty Docker configuration, cleared inherited Compose selectors and
  disabled implicit environment files with `--env-file /dev/null` and
  `COMPOSE_DISABLE_ENV_FILE=1`. No daemon connection, image pull, container or
  real settings file was involved. No format change was required. This parser
  check does not prove the subsequent Docker VM bind-mount or restore drill.

## Baseline

Run from the repository root:

```sh
just ci
```

Without just, run the same steps explicitly:

```sh
python3 scripts/check_docs.py
git ls-files -z | bash scripts/sensitive-paths.sh
test ! -e .gitleaksignore
cd checks && python3 -m pytest -q
```

Once commits exist, also run:

```sh
gitleaks git . --redact --no-banner --ignore-gitleaks-allow --config .gitleaks.toml
```

Locally the secret scan covers committed history only: `gitleaks dir .`
would also read gitignored files such as a local `.env`. Each staged change is
scanned by the pre-commit hook in a clone where `just setup` ran, and by
`git-gate` for a session; CI scans the files of its clean checkout.

The text checker reads tracked and non-ignored new files. It checks UTF-8,
LF, final newlines, trailing whitespace, simple relative Markdown links,
that no text file contains hidden Unicode, and that the agent rules file,
`.mcp.json`, agent config directories, hooks and workflows contain
no comments, long base64 blobs or symlinks. It does not validate remote links, anchors, application
behaviour, or all Markdown syntax.

Gitleaks is a required prerequisite; local verification never installs it.
CI downloads a checksum-pinned binary into the disposable runner.

Each CI run also fails while GitHub Pages is on for the repository, unless
the owner has set `PAGES_ALLOWED` in `ci.yml` to `"true"`. It reads GitHub's
API, so `just ci` has no local equivalent; check by hand with
`gh api repos/<owner>/<name> --jq .has_pages`.

## Hooks

The tracked hooks run on the owner's commits and pushes from a terminal, in a
clone where `just setup` ran. Session git writes go only through `git-gate`,
which never runs them; CI runs the same checks on every push to `ci` and every
pull request.

`just setup` writes shims into the clone's own hooks directory. Each shim
runs the tracked hook under `.githooks/` and refuses when that hook is
missing, so a commit that deletes the hooks directory is still blocked. Git
resolves its default hooks directory at run time, so the shims survive a
rename and work in linked worktrees. Only when a global or system hooks path
exists does setup pin `core.hooksPath` to this clone (absolute path): re-run
`just setup` after moving such a clone, and whenever a hook stops firing.
Health check: `just setup` prints the resolved hooks directory.

- pre-commit (also on merge commits): corruption and mass-deletion guard,
  sensitive-path refusal from `scripts/sensitive-paths.sh`, Gitleaks on the
  staged change, then `just pre-commit` when defined. `just ci` runs the same
  path list over every tracked file, so CI enforces it without the hook.
- pre-push: corruption guard, Gitleaks over the full history, then `just ci`
  when the pushed commits touch anything but documentation.

Hooks are a guardrail, not an authorisation boundary: `--no-verify` exists,
and a hooks path can be unset. Required checks and restricted write access on
the remote are the enforcement. Hooks never stash or rewrite the working tree.

## Library checks

`just checks` (part of `just ci`) runs the suite under `checks/tests/`
against `deploy/`, the workflows and the repository tooling. It needs
pytest 9.1.1 and PyYAML 6.0.3 (the `dev` extra in `checks/pyproject.toml`),
and Gitleaks on the PATH for the rule tests. Locally, install them in a
virtual environment and put it first on the PATH:

```sh
python3 -m venv checks/.venv
checks/.venv/bin/pip install pytest==9.1.1 pyyaml==6.0.3
```

GitHub CI installs the same two pins, plus pytest's own dependencies at
fixed versions, with `--no-deps`, and runs the same command. New tests
land in the same change as the code they cover. Empty test discovery for
implemented code is a failure.

Rules that hold for everything here:

- Missing tools, empty test discovery, shallow clones, skipped database
  tests and cancelled runs fail; nothing is silently skipped.
- Tests use disposable, labelled data and containers, and refuse production
  names and credentials.
- Generated files are regenerated and compared, never hand-edited.
- Any file a deploy image reads from outside its build directory has an
  explicit build context, guarded by a test; a full-checkout CI cannot catch
  this class of failure.
- A backup that has never been restored is a hope: run and record a restore
  drill after any change to backup, restore or the database image (file,
  size, integrity check, row counts, cleanup, "secret never printed").

## Result record

Record date, commit or uncommitted state, command, result, and limitations.
Local checks do not prove GitHub CI, deployment, device tests, or store
approval. Re-verify checklists against a named SHA and append dated
paragraphs; keep history.

### Library verification

- 2026-10-23, uncommitted baseline: `just docs`, `paths`, `bypasses` and
  `secrets` passed locally (no commits yet, so no history scan). GitHub CI
  not run.

### Restore drills

- 2026-10-27, commit 5a832c4 (v0.1.5 candidate: `backup.sh once`, the
  `POSTGRES_IMAGE` build argument, the flock start-up check, the cycle lock
  on its own `backup-lock` volume, pinned container paths), on a
  disposable Docker VM, arm64, Docker 29.2.1, Compose 5.1.3. Run twice:
  PostgreSQL 16 with the Dockerfile's default argument
  (`postgres:16.15-alpine`,
  sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea)
  and PostgreSQL 17 with `POSTGRES_IMAGE=postgres:17.11-alpine`
  (sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b),
  each against a server of the same image. Backup and restore-drill images
  built from the repository's Dockerfiles; `pg_dump` reported 16.15 and
  17.11. Backup service run with the environment and volumes of
  `deploy/compose.example.yml` (read-only rootfs, all capabilities dropped,
  scripts mounted read-only, a bind-mounted backup directory, the
  heartbeat and lock volumes, `BACKUP_ROOT`, `BACKUP_HEARTBEAT` and
  `BACKUP_LOCK_DIR` pinned). Result: pass for both majors. Key commands
  (`C` is `docker compose -p <project> -f <drill compose file>`, `R` the
  same for the scratch restore stack; 16 omits the build argument):

  ```sh
  docker build --build-arg POSTGRES_IMAGE=postgres:17.11-alpine deploy/backup
  C up -d backup
  C run --rm -T backup once
  C exec -T -d backup sh -c 'flock /lock/backup.lock sleep 20'
  C run --rm -T backup once
  docker compose -p <project> -f <same file without backup-lock> run --rm -T backup
  R up -d --build --wait
  printf X | dd of=<copy>/<dump> bs=1 seek=2000 conv=notrunc
  RESTORE_PROJECT=<scratch> RESTORE_SERVICE=postgres bash deploy/restore.sh <copy>/<dump>
  RESTORE_PROJECT=<scratch> RESTORE_SERVICE=postgres bash deploy/restore.sh <dump>
  psql -tA -c "select count(*), md5(string_agg(t::text, '|' order by id)) from <table> t"
  ```

  The builds ran through `C build backup` and `R up --build`, which pass
  the same argument from the compose files; the query ran on both servers.
  - Dataset: three tables (500, 3000 and 5000 rows) and a sequence; row
    counts and an md5 over each table's ordered rows matched exactly
    between the source and the restored database.
  - Loop mode: the first cycle wrote `<prefix>-*.sql.gz.enc` (about
    159 KB), its 65-byte `.hmac` and the heartbeat; `flock` and `openssl`
    present, `/bin/sh` is busybox ash.
  - Lock: `backup.lock` was created in the `backup-lock` volume, and no
    lock file appeared in the backup directory.
  - `once` with the loop container up exited 0 with its own dump. With the
    running loop container holding the lock for about 20 s, a
    `compose run backup once` waited, then dumped and exited 0 (21 s), so
    the two serialise through the volume. An unknown argument printed the
    usage and exited 2.
  - The service started without the `backup-lock` volume exited 1 with the
    message naming `backup-lock:/lock`, and wrote no dump and no `.part`.
  - Restore: the real `deploy/restore.sh`, unmodified, with
    `RESTORE_PROJECT` naming a scratch stack built from
    `deploy/restore-drill/Dockerfile` and its compose file plus a minimal
    init script for the backup role. A dump with one byte changed was
    refused at the HMAC check (exit 1, scratch database still empty); the
    untouched newest dump restored with exit 0.
  - Secret: the throwaway passphrase did not appear in the backup, `once`,
    refused-start, restore or refused-restore output.
  - Cleanup: every container, network, volume, image and directory the
    drill created was removed; images present before were kept.
  - Not verified: `deploy/restore-drill/run.sh` itself (it reads a live
    stack and uses a fixed project name, so restore.sh was driven directly
    in its place), a product's real init script and data, a missing or
    unwritable heartbeat folder, the nightly host mirror, off-site upload,
    retention pruning, the log archiver, and amd64. The bind-mounted
    directories lived inside the VM, not on a host disk.
- 2026-11-02, commit 10adbd73a141a163ae937146f0e0987fa63e50dc (v0.1.7), on a
  disposable Docker VM. Command, with `DOCKER_HOST` naming that VM:
  `bash checks/backup-restore-drill.sh <report file> <host bind folder>`.
  Result: PASS on PostgreSQL 16 (`postgres:16.15-alpine`) and 17
  (`postgres:17.11-alpine`), with the generic log archive.
  - File and size: `fakedump-*.sql.gz.enc` with its `.hmac`, 83072 bytes for
    both majors, written to a volume and to a bind-mounted host folder.
  - Integrity: a dump with one byte changed was refused at the HMAC check
    and the scratch database stayed empty; the untouched dump restored.
  - Row counts: two tables (500 and 3000 rows) and a sequence; counts and an
    md5 over each table's ordered rows matched between source and restored.
  - Log archive: one fixture log (224 bytes encrypted) decrypted back to its
    per-run line, for both majors.
  - Secret: the drill's check found neither disposable secret in its output.
  - Cleanup: the run's containers, volumes, network and images were removed.
  - Not verified: a product's real data and init script, off-site copies
    (off in this run), and the daemon's Docker version and architecture,
    which the report does not record.
- 2026-11-04, the code of commit 89ff1aae1ac30a48d0dcbef9a8487031b65f7305, on
  the same disposable Docker VM (server 29.2.1) and with the same command.
  Result: PASS on PostgreSQL 16 and 17, with the generic log archive.
  - File and size: `fakedump-*.sql.gz.enc` with its 68-byte v2 `.hmac`, 83072 bytes
    (one 83056) across both majors, written to a volume and to a bind-mounted
    host folder.
  - Integrity: a copy with one byte changed and a renamed copy were both
    refused at the authentication step; the untouched dump restored.
  - Old format: a dump with a legacy sidecar restored, with the one warning
    that its name and date are not authenticated.
  - Row counts: the checksums matched the source for the v2 and legacy
    restores, on both majors.
  - Log archive: one fixture log, 224 bytes encrypted, for both majors.
  - Secret: the drill's check passed. Cleanup: the run's containers, volumes,
    network and images were removed.
  - Not verified: a real unattended restore by `deploy/vm/vm-autostart.sh`
    (its choice of dump is covered by unit tests with the real BSD `date`
    only), a product's real data, and off-site copies (off in this run).
- 2026-11-04, deploy order on the same daemon, with compose v5.1.3 and with
  v5.6.1 inside the deployer's base image `docker:29.9.2-cli`, using a scratch
  project with one built service, one image-only service and one under a
  profile. `build` left running containers alone and skipped nothing needed;
  a failed `build` returned non-zero and changed nothing; `up -d --no-build`
  then recreated only the rebuilt service; the profile's service never
  started; `up -d --no-build` with a built image missing failed rather than
  building. Not verified: a full deploy loop run with this order.
- 2026-11-04, the code of commit e234f121a2db618fcd4157fac519a845192a4ce7, on
  the same daemon and with the same command, after the byte-exact v2 sidecar
  check and the restore flag order changed. Result: PASS on PostgreSQL 16 and
  17, with the generic log archive.
  - File and size: `fakedump-*.sql.gz.enc` with its `.hmac`, 83072 bytes (one
    83056) across both majors, written to a volume and to a bind-mounted host
    folder.
  - Integrity: a copy with one byte changed and a renamed copy were both
    refused; the untouched dump, whose sidecar ends in a newline, restored.
  - Old format: a dump with a legacy sidecar restored, with the one warning.
  - Row counts: the checksums matched for the v2 and legacy restores.
  - Log archive: one fixture log, 224 bytes encrypted, for both majors.
  - Cleanup: the run's containers, volumes, network and images were removed.
  - Last byte: inside `postgres:16.15-alpine` and `postgres:17.11-alpine`, the
    `tail -c 1 | od -An -tx1 | tr -d ' \n'` read gave `00` for input ending in
    a NUL and `0a` for input ending in a newline. Refusing a whole NUL-ended
    sidecar is covered by the unit test on the Mac, not run in those images.
  - Not verified: a real unattended restore by `deploy/vm/vm-autostart.sh`, a
    product's real data, off-site copies (off in this run), and the dump
    choice tests on Linux, which first run there when CI runs this branch.

### Shared crypto verification

- 2026-10-30, `codex/backup-crypto-mirror`, uncommitted crypto unit:
  baseline `just ci` passed 575 checks; after the shared API, consumer and
  packaging changes it passed 605 checks. Both used the existing check-suite
  environment; the default shell Python lacked pytest. No tools were installed.
  The optional product-name list was absent and the sandbox could not write
  pytest's cache; those warnings do not establish product-specific scanning.
- The 25 direct crypto regressions cover independent old-format fixtures,
  old-algorithm decryption of new output, bad configuration and sidecars,
  changed/truncated ciphertext, wrong passphrase/salt, withholding plaintext,
  private snapshots, existing outputs and handled signals at both publication
  steps. A valid MAC over undecryptable ciphertext emits no plaintext.
- Independent security review found a signal window after ciphertext
  publication; inode-aware cleanup fixed it. The rerun proved that handled
  interruption leaves neither member of the pair and does not delete a
  competing writer's output. Review also tightened the operator drill's
  Compose/restore environment isolation and resource collision checks.
- Container restore evidence remains outstanding in this environment: the
  default Docker socket was unavailable, and no other daemon or VM was
  selected or changed. `checks/backup-restore-drill.sh` is the operator gate
  for actual PostgreSQL 16/17 and generic archive round-trips; its packaging
  and environment isolation are checked locally, which is not a container
  or restore result. No real credentials, database or archive was used.

### Explicit mirror sources verification

- 2026-10-30, `codex/backup-crypto-mirror`, uncommitted mirror unit after
  `ce38109`: `just ci` passed 632 checks. The original and extra-source
  mirror suites passed 69 checks, then an independent reviewer reran all
  69 successfully. Source scripts, installed-copy fixtures and the existing
  LaunchAgent/settings contract are covered; no second job was added.
- Cases include two sources with duplicate basenames, spaces, absent mounts,
  missing folders/sidecars, namespace collisions, canonical ancestry,
  symlinks, user ownership/modes, interrupted publication, differing bytes
  at the same size, truncated existing copies, family-only retention and
  a single failure-to-recovery transition. Existing corrupt immutable copies
  now fail visibly rather than being silently skipped or overwritten.
- Independent security review approved the copy/path/cleanup/state changes
  with no remaining findings. The same optional-name-list and pytest-cache
  warnings noted above remain. Real mounted-disk copying, LaunchAgent loading,
  provider alerts and the container restore drill remain operator checks;
  local fixtures are not evidence of those external operations.
