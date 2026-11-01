# Dependencies and licences

## Policy

- The lockfile is the inventory. Record here only what a lockfile cannot
  see: vendored assets (fonts, icons, colour profiles, images), binary SDKs
  (xcframeworks, AARs), and CC-BY provenance fields.
- Shipped code and anything bundled into a build may use only the allowed
  licences below. AGPL, GPL, LGPL, SSPL, BUSL, Fair Core, "source-available"
  and commercial-only code never enter shipped code and are never modified.
  Running such a tool unmodified as a separate process in CI is allowed when
  its licence permits it; record that use here.
- Compound SPDX expressions: `A OR B` passes when any option is allowed;
  `A AND B` requires every part to be allowed.
- THIRD_PARTY_NOTICES.md is generated at release by the stack's tool, with
  hand entries for vendored assets only. The licence gate runs in `just ci`
  once the stack is configured:
  - Node: `pnpm licenses list --prod --json` checked against the allowlist
  - Python: `pip-licenses --allow-only "<allowlist>"` inside the project venv
    (running it under a uv-managed venv is unverified)
  - Android: AboutLibraries Gradle plugin in strict mode
  - iOS and macOS: LicensePlist reading `Package.resolved` (SwiftPM has no
    first-party licence command)
  - Flutter: the built-in NOTICES asset and `showLicensePage()`
- Obligations beyond a notice: Apache-2.0 components' NOTICE files ship
  verbatim; OFL fonts keep their reserved font name and `OFL.txt`; vendored
  MPL-2.0 files are never edited (fork the file under its own licence if a
  change is needed); CC-BY assets carry author, title, source URL, licence
  and a note of modifications in the register.
- No runtime fetches from third-party CDNs. A dependency that defaults to a
  CDN is configured to bundle, and that setting is re-checked on upgrade.
- Commit the ecosystem's lockfile. Install with `--frozen-lockfile`,
  `npm ci`, `uv sync --locked` or the stack's equivalent. Never install
  during verification.
- Dependency-bot PRs are notifications: apply the bump locally, run `just ci`,
  commit through the normal path.
- Check transitive obligations when a direct dependency changes licence or
  brings in a copyleft transitive.

## Allowed licences (shipped code)

MIT, ISC, 0BSD, BSD-2-Clause, BSD-3-Clause, Apache-2.0, MPL-2.0, OFL-1.1,
CC0-1.0, CC-BY-4.0, Unlicense, BlueOak-1.0.0, Python-2.0, Zlib.

Add to this list only with a dated entry in DECISIONS.md. Do not copy another
project's exceptions.

## Register (vendored assets and binary SDKs only)

Nothing is vendored yet. The baseline uses Python 3.11+ standard-library
APIs, Git, Gitleaks and `just`; CI uses an immutable checkout-action pin and a
checksum-pinned Gitleaks binary, which are reproducibility choices reviewed
before updating.

| Asset | Version | SHA-256 | Purpose | Licence / evidence | Attribution fields | Review date |
| --- | --- | --- | --- | --- | --- | --- |

## Tools, actions and images

Run unmodified as separate processes: in CI, in the check suite, or as the
base of the deploy images. None is vendored. Licences marked "upstream"
were not checked here; the Python ones were read from the installed
packages' metadata on 2026-10-23.

| Component | Version / pin | Used by | Licence |
| --- | --- | --- | --- |
| pytest | 9.1.1 | check suite | MIT (metadata) |
| PyYAML | 6.0.3 | check suite | MIT (metadata) |
| iniconfig, packaging, pluggy, Pygments | 2.3.1, 26.3, 1.7.0, 2.21.0 | pytest's dependencies in CI | MIT; Apache-2.0 OR BSD-2-Clause; MIT; BSD-2-Clause (metadata) |
| wrangler | 4.131.1 (`wrangler-version` input, run through npx) | reusable-worker-deploy | MIT OR Apache-2.0 (upstream) |
| actions/checkout | v7.0.1, by commit SHA | workflows | MIT (upstream) |
| actions/setup-python | v7.1.0, by commit SHA | workflows | MIT (upstream) |
| actions/setup-node | v4.4.0, by commit SHA | reusable workflows | MIT (upstream) |
| actions/cache | v6.1.0, by commit SHA | reusable-ci (cargo cache) | MIT (upstream) |
| pnpm/action-setup | v6.1.0, by commit SHA | reusable workflows | MIT (upstream) |
| Gitleaks | 8.30.1, by SHA-256 | CI, hooks, check suite | MIT (upstream) |
| Rust toolchain | the caller's exact X.Y.Z, via the runner image's rustup over HTTPS (not checksum-pinned) | reusable-ci (`rust-toolchain`) | MIT or Apache-2.0 (upstream) |
| postgres image | 16.15-alpine | backup and restore-drill images | PostgreSQL Licence (upstream) |
| docker image | 29.9.2-cli, by digest | deployer image | Apache-2.0 (upstream) |

This document is not a licence grant.
