# Shared backup encryption

`deploy/backup/crypto.sh` is a source-only shell module. Importing it performs
no validation, file I/O, scheduling or network work. It does not read settings,
run PostgreSQL or require Docker. Call it from Bash or the backup image's ash:

```sh
. <library>/deploy/backup/crypto.sh
backup_encrypt /private/archive.tar.gz.enc < /private/archive.tar.gz
backup_verify /private/archive.tar.gz.enc
backup_decrypt /private/archive.tar.gz.enc > /private/restored.tar.gz
```

The caller supplies `BACKUP_PASSPHRASE` through its protected environment and
`BACKUP_HMAC_SALT`, exactly eight bytes, unchanged from its existing backups.
Neither belongs in an argument or a log. OpenSSL and standard shell file tools
must be installed. Each function exits non-zero on refusal or failure.

- `backup_encrypt OUTFILE [FINAL_NAME]` reads arbitrary bytes from stdin and
  creates `OUTFILE` and `OUTFILE.hmac`, mode 600. It refuses an existing output or
  sidecar, including symlinks. A private working directory is placed beside
  the output; publication uses same-filesystem links without overwriting.
  The sidecar appears first, ciphertext last as the completion marker.
  With `FINAL_NAME` the sidecar is v2, bound to that name, so a transient
  `OUTFILE` verifies only once it has been renamed to it. Without it the
  sidecar is legacy.
- `backup_verify FILE` verifies a private snapshot of ciphertext and sidecar
  without decrypting or producing stdout.
- `backup_decrypt FILE [--warn-legacy|--require-v2]` verifies that snapshot, decrypts it
  completely into a private temporary file, then writes plaintext to stdout. Failed integrity
  or decryption produces no plaintext. A source change after verification
  cannot change the snapshot being decrypted. `--warn-legacy` prints one
  warning line on stderr when the snapshot's sidecar has the legacy form;
  `--require-v2` refuses that form, with no plaintext.

The helper sets `umask 077`: new ciphertext and sidecars are owner-only
(`0600`), and work directories are `0700`. Earlier wrappers running with
`umask 022` could produce `0644` files. This permissions change does not alter
existing files, but restore, mirror and upload readers must have access as the
owning user; verify the consumer's reader identities before adopting it.

Protect the output directory and temporary storage from other writers. The
functions use private mode-700 work directories and remove them on normal
exit and handled signals. SIGKILL or a power loss cannot run cleanup: the
operator must remove abandoned `.backup-crypto.*` or `backup-crypto.*`
directories before reusing storage. Decryption temporarily needs room for
both a ciphertext snapshot and the plaintext. Callers must protect any
plaintext they redirect or decompress themselves; this API does not erase
storage blocks or make environment variables unreadable to the same user.

A producer's failed pipeline is visible only to its caller. Use pipefail and
a transient output name, then check the complete pipeline before publishing
an application artifact. The database and log wrappers do this, and retain
their existing size checks, retention, locks, heartbeats and upload behaviour.
An unpaired sidecar after an uncatchable interruption is not a complete
artifact. Do not treat it as one.

## Compatibility

The encryption is unchanged: AES-256-CBC with a random OpenSSL salt and its
PBKDF2 defaults. So is the MAC key: it is derived separately from the same
passphrase using the existing eight-byte label salt and OpenSSL's AES-256-CBC
PBKDF2 derivation. The `.hmac` sidecar has two forms under that key, each
holding a lowercase hexadecimal HMAC-SHA-256:

- v2, `v2 <MAC>` and a newline, exactly 68 bytes. The MAC covers the line
  `backup-crypto-v2`, a line with the file's base name, then the ciphertext, so
  it authenticates the bytes, the name and the date the name carries. A name
  holding `/` or a newline is refused. `backup.sh` writes this form.
- Legacy, `<MAC>` and a newline. The MAC covers the ciphertext alone, so it
  authenticates the bytes only and a renamed file still verifies. Whitespace is
  stripped before this form is read, as it always was. One-argument
  `backup_encrypt` stays legacy; the log archives use it.

`openssl enc -salt` starts every output with `Salted__`, and authentication
refuses a file that does not start with it. So a v2 input never equals a legacy
input, and a v2 sidecar with its marker removed cannot pass the legacy check,
even over a file holding that v2 input.

- The new library restores old dumps and log archives.
- The old library refuses a v2 sidecar as malformed, an authentication failure,
  so a rollback needs the new `restore.sh` to read the dumps written since.
- A v2 copy under another name is refused. Restore it under its original name.
- The sidecar's file name is still `.hmac`, so mirrors and off-site copies keep
  working: they copy and compare bytes and never read the form.

The passphrase is passed to OpenSSL with `-pass env:BACKUP_PASSPHRASE`.
As in the old implementation, the derived MAC key does appear in an internal
OpenSSL `-macopt` argument. Never enable shell tracing around secret handling;
the public functions turn tracing off inside their subshells. A process able to
read the command line, which can include other users depending on the platform,
could catch the key in argv while a backup or restore runs and forge a sidecar,
including one for a renamed dump. The key depends only on the
passphrase and the salt, so that holds for every dump under them, not only the
one being handled.

Compression, SQL headers/trailers and archive validation belong to callers.
Authenticated restores through `deploy/restore.sh` refuse a symlinked dump or
`.hmac` sidecar before any SQL is applied: both must be plain files. Older
wrappers could follow these links; the shared authentication helper now fails
closed instead.
Only `deploy/restore.sh` retains the historical explicit `RESTORE_UNVERIFIED=1`
opt-in for a dump without a sidecar. The shared API has no authentication bypass.

The automatic restore in `deploy/vm/vm-autostart.sh` uses only dumps with a v2
sidecar, and chooses and ages them by the UTC date in the file name, never by a
file time. It passes `RESTORE_REQUIRE_V2=1`, so `restore.sh` itself refuses a
legacy dump at authentication, and a dump with no sidecar whatever
`RESTORE_UNVERIFIED` says. That variable is internal, not a product setting. A
legacy dump is restored by hand with `deploy/restore.sh`, which prints one
warning: the encrypted bytes are verified, the name and its date are not. For a
restore by hand `RESTORE_UNVERIFIED` is unchanged.

What this leaves open: someone with access to the disk can still delete newer
dumps, and the older one left is then the newest, bounded only by the age cap.
A dump skipped or passed over at the choice step (its name dated more than 600
seconds ahead or holding no real date, its marker changed or its sidecar
removed) has the same effect: the next older v2 dump is chosen. The log says
which dump and why, and the login alert for the restore gives how many there
were, when there are any. That gives no more than deleting the dump. Renaming
the newest dump to an older date, or out of the dump pattern, has the same
effect as deleting it too: the next older one is chosen and no trace is left,
with no log line and no count in the alert. A name or marker forged on the dump
actually chosen makes `restore.sh` refuse, with no fall back.
Pruning and the mirror's retention windows still use file times.

The name binding helps only when the dumps are on a different disk from the
checkout and `.env`. `BACKUP_DIR` defaults to `<repo>/backups`. On that layout
someone holding the disk can read the passphrase and forge a sidecar, or edit
the `restore.sh` that login runs, so v2 adds nothing against them. Set
`BACKUP_DIR` to another disk for this protection.

## Packaging

Backup and log archive services mount the complete `deploy/backup` directory
at `/scripts`, so their sibling `crypto.sh` is available on every run. A host
using `deploy/restore.sh` needs `deploy/backup/crypto.sh` at that same relative
location. A product image that copies only `restore.sh` must also copy the
helper to `backup/crypto.sh` beside it. Keep the versioned library intact;
never copy a second maintained encryption implementation into a product.

## Disposable container drill

From a sandboxed session with Docker Compose, Git, OpenSSL and Python available:

```sh
DOCKER_HOST=unix:///path/to/disposable/docker.sock \
  bash checks/backup-restore-drill.sh /tmp/backup-drill-report.log /srv/drill-shared
```

This builds the actual backup Dockerfile with the shared helper and wrappers,
then exercises PostgreSQL 16 and 17 against matching pinned database images.
For each major it restores the real dump through `deploy/restore.sh`, compares
table counts, ordered-row checksums and a sequence, refuses a tampered dump
before any SQL is applied, and decrypts a real log archive. Only synthetic
fixtures are used. Builds may download the pinned images and Alpine packages.
The second argument is an existing, user-owned host directory shared with the
Docker VM and not writable by other users. Each run creates its own private
child there, prints the bind folder, and proves bidirectional host/container
visibility and hard links before starting the databases. It writes backups
into both a named volume and that host bind, then restores the bind-mounted
backup. A missing share or unsupported hard link fails the drill. Only its
own scratch child is removed; the supplied parent is preserved.
It creates uniquely named containers, volumes, networks and a temporary image
on the explicitly selected Docker daemon; it refuses an absent `DOCKER_HOST`,
clears `DOCKER_CONTEXT`, and prints the daemon's name before creating drill
resources. It exposes no host ports and removes
its resources on exit. Previously existing images are kept. It neither reads
an existing database nor starts or stops a VM. The report records the results;
a unit suite is not a substitute for this container drill.

The daemon name is announced immediately. Other output is captured in a private
file, published atomically to the supplied report and printed after cleanup.
The report identifies the full source commit and ends in exactly
`PASS: backup restore drill` or `FAIL: backup restore drill`, including preflight
and cleanup failures. Logging uses regular files, without `/dev/fd` paths.
