# Security

## Reporting

Report vulnerabilities to `contact@neuraitech.com`; never in a public
issue. Include the affected file and tag, steps to reproduce, impact and
any proof of concept; never include live credentials or personal data. We
acknowledge within 3 business days and triage on a best-effort basis; we
have no bug bounty. Researchers acting in good faith (no data exfiltration,
no service disruption, no access beyond what proves the issue) will not face
legal action from us. We ask for up to 90 days before publication, as a
ceiling on disclosure rather than a promise of a fix date. Out of scope:
third-party services we use, social engineering, denial of service.

## Secrets

- Credentials, private keys, signing keys, service-account files, upload
  keystores, runtime data, dumps and logs never enter Git. `.gitignore` is the
  first layer. In a clone where `just setup` ran, the pre-commit hook refuses
  sensitive paths even when force-added, and the hooks run Gitleaks on the
  staged change and on the full history before a push; session git writes go
  through `git-gate`, which runs no hooks. CI is what enforces: on every push
  to `ci` and every pull request it refuses sensitive paths and scans the full
  history. A missing scanner fails the gate.
- The path list in `scripts/sensitive-paths.sh` is the single definition the
  hook and `just ci` share; `.gitignore` mirrors it. A gitignore entry alone
  is bypassed by `git add -f`.
- `.gitleaks.toml` adds rules for providers the defaults miss (Supabase,
  Resend, Razorpay). Every scan runs with inline allow comments ignored, in
  the hooks, `just ci` and CI alike, and a `.gitleaksignore` file is refused
  by the protected hooks themselves as well as by `just ci` and CI, so
  neither can silence a finding anywhere in history.
- Scanner allowlists hold only verified false positives; `.gitleaks.toml`
  has one, CocoaPods podspec checksums in `Podfile.lock`, by owner decision
  (docs/DECISIONS.md). Add another only the same way, narrowly: a path AND a
  line regex, limited with `targetRules` to the rule
  that misfires (a global allowlist that names a path makes `gitleaks dir`
  skip the whole file), with the reason in the config. Never exempt the
  config file itself or the files that define secret patterns.
- Test fixtures that look like secrets are split at construction time so
  they do not match a scanner pattern.
- If a secret leaks, follow [docs/INCIDENT.md](docs/INCIDENT.md): revoke and
  rotate first, redeploy every consumer in the same change, read the
  provider's audit log, then decide on a history rewrite. Assume the old key
  stays live until the provider confirms otherwise. Deleting the file does
  not remove it from history.

## Authorisation and data

- Least privilege and private defaults. Test authorisation at the server or
  database boundary, including access by another user or tenant. Hiding a UI
  control is not authorisation.
- Fail closed: when a dependency such as a risk or auth service is
  unreachable, reject; never approve by default.
- Money crosses APIs as decimal strings and is computed server-side.
- Read-only surfaces (dashboards, monitors, analysts) never write to the
  state they observe.
- Review data collected, storage, access, retention, export and deletion
  before shipping; the privacy policy must match the code, and a test should
  hold them together. This library collects no data; each consuming
  product keeps its own data inventory.
- Destructive commands carry an explicit scope knob (project name, DB name
  prefix) and refuse production names.

## Agents and machines

- Agents that execute arbitrary commands run only inside a VM, as an
  unprivileged guest user with no sudo and no docker socket. Host file
  permissions do not restrain a guest across a mount; keep secrets off every
  mount and push them in explicitly. Mount the minimum, read-only, and verify
  with `findmnt` after boot.
- `.claude/settings.json` turns the OS sandbox on for this repository,
  denies reads of secret files at the syscall level, and denies sandboxed
  writes to the tracked hooks, the scanner config and the path gate, since
  those run later outside the sandbox, in the owner's terminal and in CI. A
  `Bash` deny alone is not a boundary (`bash -c` walks past it); the sandbox
  is. The `justfile` and `scripts/check_docs.py` stay writable and the owner's
  hooks run them, so their diffs get a human look. Keep the read lists in step
  with
  `scripts/sensitive-paths.sh`.
- Repository content is untrusted input to an agent. `just ci` fails on
  hidden Unicode (control, format, private-use, unassigned, separator and
  variation-selector characters) in every text file, and on HTML or
  reference-style comments, long base64 blobs and symlinks in the agent
  files, `.mcp.json`, agent config directories, hooks and workflows,
  because those are the files an agent reads as instructions.
- Machine-side, because no repo file can enforce them: `git config --global
  core.fsmonitor false` (a hostile `.git/config` can otherwise run a command
  when an agent opens the repo); open any repository you did not write with
  `claude --bare`; keep transcript retention short and review auto-memory
  files separately.
- MCP servers, skills and plugins are a supply chain. This repository ships no
  `.mcp.json`; any server a project adds is pinned to an exact version and
  listed in `enabledMcpjsonServers`, never enabled wholesale. Read a
  third-party skill or hook script in full before installing it.
- Nothing is tunnelled or exposed on a public port without real
  authentication in front of it. An unauthenticated loopback service is
  reachable from any local process and from any browser tab the operator has
  open.
- An agent holding an admin API token can undo runtime hygiene settings; the
  network bind and a proxy are the only real controls.
- Never export globally an environment variable another tool honours
  (`LIMA_HOME`, `DOCKER_HOST`, `COMPOSE_PROJECT_NAME`). Use a scoped alias.
  Never start, stop or inspect another project's VMs.
- Production services never share a disk with heavy builds or experimental
  workloads, and an external uptime monitor watches them. Every alert path
  that lives inside the VM dies with it.
- An agent sandbox refuses writes to `.git/hooks` and `.git/config` by
  design: a confined process must not install code that later runs
  unconfined. `just setup` runs from a normal shell.

## CI and deployment

- `permissions: contents: read` at the workflow level; grant more only on the
  job that needs it. On a private repository a scanner action that reads PRs
  needs `pull-requests: read`, or it fails with a 403 that looks like a leak.
- Actions pinned to a commit SHA with a version comment; Dependabot keeps
  them current behind a 7-day cooldown. Tools downloaded in CI are
  checksum-pinned; a tag can move, a digest cannot. No `version: latest`.
- Package managers hold new releases back (about a day) and never run
  dependency lifecycle scripts by default. A
  compromised release is usually pulled within hours, and a lockfile alone
  does not stop a fresh lock from taking it.
- `persist-credentials: false` on every checkout that does not push.
  Deployment and signing credentials never reach a job that runs untrusted
  PR code. `pull_request_target` is never used.
- CI secrets (deploy tokens, API keys) are environment secrets: a GitHub
  environment whose deployment branches are `main` only (Selected branches
  and tags, one branch rule `main`) holds them, and only the job that needs
  them names it, so a run on any other branch cannot reach them. Repository
  and organisation secrets have no branch restriction, so a workflow pushed
  to any branch can read them; no secret lives there, and no repository
  secret keeps the same name once it moves. Anything merged to `main` still
  reaches the environment, through the job that names it and everything it
  runs. Environments on a private repository need GitHub Pro for a
  user-owned one, Team or Enterprise for an organisation's; without that, a
  deploy that needs secrets runs from the operator's machine, from a clean
  checkout of the merged `main` commit, with the secret outside any path an
  agent can read, not from Actions.
- A deploy or promotion workflow requires `event == push`, the exact branch,
  and `head_repository == github.repository`: a fork branch can be named
  `main`. Key gates on the workflow path, not the display name.
- `cancel-in-progress: true` on checks, `false` on deploys. A cancelled
  required check reports cancelled, which blocks the merge until it is
  re-run; that fails safe.
- A Postgres service in CI trusts every connection; that is safe only on a
  disposable hosted runner.
- Dependabot PRs run CI with a read-only token and no secrets, and are never
  merged from the button.
- GitHub Pages on a private repository publishes a public site (private
  Pages needs GitHub Enterprise Cloud), and a branch-root source serves every
  committed file. Pages stays off unless the repository is a website that
  publishes built files only. The baseline workflow fails on the next pull
  request or push while Pages is on, unless `PAGES_ALLOWED` in `ci.yml` is
  `"true"`; only the owner sets it, with a decision entry, never an agent to
  make CI pass. Off means `has_pages` is false: Unpublish is undone by the
  next deploy, and deleting `CNAME` only removes the custom domain. Before
  a site with its own custom domain (set in that repository's Pages
  settings, not inherited from the account's site) goes off, delete at the
  DNS provider the records of that name, with and without a leading
  `www.`, whose value is a GitHub Pages address or `<owner>.github.io`,
  keep the `_github-pages-challenge-<owner>` TXT record, and wait out those
  records' TTL; turn Pages off only after that. A domain belongs to one
  Pages site at a time, so in that order nobody can claim it in between,
  whereas a record left pointing at a disabled site lets another GitHub
  user claim the domain. Verifying the domain in the account's Pages
  settings covers only it and its immediate subdomains, and a wildcard
  record stays at risk even then. Confirm the clean-up in the provider's
  record list: `dig` shows only A records by default and cannot see behind
  a proxied record.
- Server-side enforcement: `.github/rulesets/main.json` blocks deletion and
  force-push on `main`, requires linear history and the CI check, with no
  bypass actors. Apply it with the command in
  docs/PROJECT.md; the check is pinned to the GitHub Actions app so a
  hand-posted status cannot satisfy it. `.github/rulesets/tags.json`
  makes release tags `v*` immutable: no deletion, no update, no force.
  Rulesets are not inherited: both are applied once the repository is on
  GitHub. Changes to `.github/`, `.githooks/`, `scripts/`, `.gitleaks.toml`,
  `deploy/` and `checks/` (`.github/CODEOWNERS`) are reviewed by hand
  before they are pushed to `ci`. That review is policy, not enforced by the
  ruleset, which has no pull-request rule. Hooks remain a
  guardrail, not an authorisation boundary. GitHub's paid scanners (secret
  protection, CodeQL, dependency review) are not assumed on private
  repositories.

## Current enforcement

`just setup` installs the tracked hooks, which run on the owner's commits and
pushes from a terminal; `git-gate` never runs them. `just ci` runs the
documentation and hidden-content check, the sensitive-path check, the Gitleaks
bypass check, and Gitleaks over the current tree and the history. The GitHub
baseline workflow, on every push to `ci` and every pull request, does the same
after a full-history checkout, and on each run also fails
while GitHub Pages is on (unless `PAGES_ALLOWED`), a check that needs
GitHub's API and so has no local equivalent. Secret scanning does not
replace application security tests or human review.
