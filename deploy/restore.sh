#!/bin/bash
# bash, not sh: this runs on the host, and dash aborts on `set -o pipefail`.
# Restore one encrypted backup into the running postgres container.
#
#   PRODUCT_SETTINGS=<path>/product.settings BACKUP_PASSPHRASE=... ./restore.sh backups/<prefix>-20260817-020000.sql.gz.enc
#
# Restores INTO the live database, additively: over a populated database this
# produces duplicate-key errors, not a clean overwrite. For a from-scratch
# recovery, bring up a fresh postgres first - THE COMPOSE-DEFINED ONE
# (docker compose up -d <DB_SERVICE>), not the bare image: the dump's OWNER and
# GRANT statements name the product's app and backup roles, which come from
# postgres-init at first initialisation and not from the dump, and a bare
# postgres refuses the restore on the last of them after applying everything
# else. Then run this, then start api/worker.
#
# Each product records its own drill runs in its docs. Run one with
# restore-drill/run.sh after touching backup.sh, this file, postgres-init or
# the image pin. A backup that has never been restored is a hope, not a backup.
#
# Nothing reaches the database until the file has been proven whole: the
# .hmac sidecar backup.sh writes must match (a modified, truncated, renamed or
# wrong-passphrase file stops here), then the dump is decrypted into a
# private temp file and checked for pg_dump's header and trailer. Only then
# does psql run, with ON_ERROR_STOP + --single-transaction so a mid-stream SQL
# error still applies nothing.
#
# The sidecar binds the dump to the file name given here, and so to the date
# in that name. An old-format sidecar authenticates the bytes only: such a dump
# still restores, with one warning line, and a rename of it goes unnoticed.
# RESTORE_REQUIRE_V2=1 refuses it instead, and refuses a dump with no sidecar
# whatever RESTORE_UNVERIFIED says: vm-autostart sets it, since its age cap
# trusts the date in the name. A restore by hand leaves it unset.
#
# Two temp paths hold plaintext during a restore, both under ${TMPDIR:-/tmp}:
# <PRODUCT>-restore.* (the whole dump) and backup-crypto.*/plaintext (gzipped,
# from backup_decrypt). vm-autostart sets TMPDIR from RESTORE_WORK_DIR; this
# script does not read that setting. A manual restore on the host should set
# TMPDIR itself to keep the plaintext off the default temp folder.
set -eu
set -o pipefail
# The caller's two choices, taken before sourcing, so a stray RESTORE_REQUIRE_V2
# or RESTORE_UNVERIFIED line in the settings does not change what the caller set.
# This guards against a mistake, not a hostile file: the settings file is sourced
# shell, trusted as much as this script, and a line in it can still set these
# captured variables or anything else. RESTORE_PROJECT and RESTORE_SERVICE are
# not captured: they are read after sourcing, so a settings line replaces them.
caller_require_v2="${RESTORE_REQUIRE_V2:-}"
caller_unverified="${RESTORE_UNVERIFIED:-}"
# Settings: the product's product.settings, named by PRODUCT_SETTINGS (vm-autostart
# passes its installed copy; the drill passes the one it was given).
# shellcheck source=/dev/null
. "${PRODUCT_SETTINGS:?PRODUCT_SETTINGS must name the product.settings of the product}"
: "${PRODUCT:?}"
: "${COMPOSE_PROJECT:?}"
: "${DB_NAME:?}"
: "${DB_USER:?}"
# BACKUP_HMAC_SALT must match backup.sh; changing it invalidates the .hmac
# sidecar of every existing dump. Optional settings and their defaults follow:
# RESTORE_SERVICE, like RESTORE_PROJECT, lets the drill name its own service.
: "${DB_SERVICE:?DB_SERVICE must be set (product.settings)}"
SERVICE="${RESTORE_SERVICE:-$DB_SERVICE}"
: "${BACKUP_HMAC_SALT:?BACKUP_HMAC_SALT must be set (product.settings)}"
# openssl enc -S keeps only 8 bytes of salt, so a longer or shorter value would
# not mean what it says; refuse anything but exactly 8 bytes.
[ "$(printf %s "$BACKUP_HMAC_SALT" | wc -c | tr -d ' ')" -eq 8 ] || { echo "REFUSED: BACKUP_HMAC_SALT must be exactly 8 bytes" >&2; exit 1; }
ERASURE_CHECK_SQL="${ERASURE_CHECK_SQL:-}"

file="${1:?usage: BACKUP_PASSPHRASE=... ./restore.sh <backup-file>}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set}"
# The live stack is project COMPOSE_PROJECT; the restore drill points this at a
# scratch project so a dump is proven restorable without touching production.
PROJECT="${RESTORE_PROJECT:-$COMPOSE_PROJECT}"
# Say which stack this is about to write to, before it writes to it. The
# default is the live one, so a drill that forgets the variable restores over
# production and looks like it passed - it proves nothing, though it destroys
# nothing either, because psql runs --single-transaction with ON_ERROR_STOP
# and aborts on the first duplicate key.
#
# Announced rather than made mandatory on purpose: requiring the variable puts
# a hard failure on the emergency path, where someone restoring under pressure
# would meet exit 2 with an empty database, in order to prevent a soft failure
# on the drill path. One line of output buys the same certainty.
echo "restoring into compose project: ${PROJECT}" >&2

# The shared helper verifies before releasing plaintext. Only this wrapper
# retains the explicit legacy opt-in for dumps predating authenticated sidecars.
. "$(dirname "$0")/backup/crypto.sh"
decrypt_option=--warn-legacy
[ "$caller_require_v2" != 1 ] || decrypt_option=--require-v2
verified=1
if [ ! -f "${file}.hmac" ]; then
    if [ "$caller_require_v2" = 1 ]; then
        echo "REFUSED: ${file}.hmac is missing, and this restore requires a v2 sidecar." >&2
        exit 1
    fi
    if [ "$caller_unverified" != 1 ]; then
        echo "REFUSED: ${file}.hmac is missing. Dumps written before the sidecar existed have none; set RESTORE_UNVERIFIED=1 to restore one anyway." >&2
        exit 1
    fi
    verified=0
fi

# 2. Decrypt and decompress to a private temp file, then check it is a whole dump.
umask 077
plain=$(mktemp "${TMPDIR:-/tmp}/${PRODUCT}-restore.XXXXXX")
trap 'rm -f "$plain" || :' EXIT
if [ "$verified" = 1 ]; then
    if ! backup_decrypt "$file" "$decrypt_option" | gunzip >"$plain"; then
        echo "REFUSED: dump authentication or decryption failed" >&2
        exit 1
    fi
else
    openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$file" | gunzip >"$plain"
fi
head -10 "$plain" | grep -q '^-- PostgreSQL database dump$' || {
    echo "REFUSED: decrypted file does not start like a pg_dump" >&2
    exit 1
}
# pg_dump 16.10+ ends with \unrestrict after the trailer; leave room for more.
tail -20 "$plain" | grep -q '^-- PostgreSQL database dump complete$' || {
    echo "REFUSED: dump is truncated (no 'database dump complete' trailer)" >&2
    exit 1
}

# 3. Only now touch the database, atomically.
docker compose -p "$PROJECT" exec -T "$SERVICE" psql -U "$DB_USER" -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 --single-transaction <"$plain"

# The whole database in cleartext, and step 4 below adds a docker round-trip
# with no timeout - a slow postgres there would keep the plaintext on disk for
# as long as it hangs. The trap stays for every path that never gets here.
rm -f "$plain" || :

echo "restore of $file into compose project ${PROJECT} complete" || :

# No ERASURE_CHECK_SQL: the product has no erasure record to check, so step 4
# and its notice are skipped.
[ -n "$ERASURE_CHECK_SQL" ] || exit 0

# 4. Say what the restore silently undid.
#
# The erasure record (read by ERASURE_CHECK_SQL) lives in this same database, so a dump taken before an
# erasure request brings the person's data back AND takes the record that they
# asked away with it. Nothing downstream then knows to re-run the purge, and
# nothing can work out who: the row holds a salted hash rather than the id
# (deliberately - a record proving we erased someone must not retain them),
# and deletion.py logs that a callback was processed, never whose.
#
# The restored data does know one thing, and it is the useful one: the newest
# erasure request it contains. Everything after that timestamp is undone.
# Deliberately NOT coalesced to a string: an empty table is not a timestamp,
# and folding it into the timestamp slot produced "after none recorded", which
# reads as nothing to do while meaning the widest possible loss. The reason
# for a failed read is kept too - "the table is not in this dump" and
# "postgres is not up yet" need opposite responses and looked identical.
# Everything from the psql above down is advisory and runs AFTER the point of
# no return, so nothing in it may change the exit status - which is why the rm
# and the echo above end in `|| :`, and why both EXIT traps do. vm-autostart
# calls this script as a bare command with RESTORE_OK still 0, so a non-zero
# exit fires a cleanup that removes the volume this restore just filled: a
# successful restore must not be discarded because the warning about it could
# not print. Measured on bash 3.2.57, the host's: under `set -eu` a failing
# EXIT trap sets the status too, and rm fails when the unlink does - a
# read-only or full disk, which is when this script runs.
#
# So no second mktemp. $plain came from one under umask 077, so "$plain.err"
# is a private name that exists whenever this line runs, and a name cannot
# fail. Replaces the trap set at the top - a second `trap ... EXIT` overwrites
# the first rather than adding to it, and the plaintext of the whole database
# was leaking until a test that has guarded that since before this change
# said so.
erasure_err="${plain}.err"
trap 'rm -f "$plain" "$erasure_err" || :' EXIT
# Only the paths that write an erasure record row are bounded by this
# timestamp; a purge that writes none is not - conservatively, since an
# older boundary widens the window rather than narrowing it, but a reader
# will assume it covers every route unless told.
erasure_boundary() {
    docker compose -p "$PROJECT" exec -T "$SERVICE" psql -U "$DB_USER" -d "$DB_NAME" \
        -tAc "$ERASURE_CHECK_SQL" \
        2>"$erasure_err" | tr -d '\r'
}
# EXIT STATUS, not "is stderr empty". docker compose warns about every unset
# ${...} in docker-compose.yml - three of them have no default and this script
# loads no env file - so stderr is populated on a perfectly successful run,
# and treating that as failure sent the empty-table case, the widest one
# there is, down the "unknown" branch blaming POSTGRES_PASSWORD.
if newest_erasure="$(erasure_boundary)"; then read_ok=1; else read_ok=0; fi
# Last line, and compose's warnings dropped: psql's "relation does not exist"
# is what a person needs, and it arrives after the noise, not before it.
why="$(grep -v 'level=warning\|^WARN\[' "$erasure_err" 2>/dev/null | tail -1 || true)"

if [ "$read_ok" = "1" ] && [ -n "$newest_erasure" ]; then
    boundary="Newest erasure request in the restored data: ${newest_erasure}
  Any data-deletion request received AFTER that is gone from this database."
elif [ "$read_ok" = "1" ]; then
    boundary="NO erasure request survives in the restored data.
  This cannot tell you whether none was ever received or all of them are gone.
  Assume the second."
else
    boundary="Could not read the erasure record (ERASURE_CHECK_SQL), so the window is unknown: ${why:-no reason reported}
  If the table is absent, this dump predates the feature and every request
  since is undone."
fi

# The rehearsal the header asks for restores into a scratch project, and an
# alarm that cries wolf on every drill is one the operator learns to dismiss.
# Compared with COMPOSE_PROJECT, so it follows the live stack's name.
if [ "$PROJECT" = "$COMPOSE_PROJECT" ]; then
    consequence="Until step 1 is done, this deployment is holding data that someone asked us to delete."
else
    consequence="This is compose project '${PROJECT}', not the live stack, so nothing is owed here."
fi

cat >&2 <<NOTICE || true

WARNING: this restore may have undone erasures.
  ${boundary}
  The data those requests erased is back, and the record of each request went
  with it.

  You CANNOT list them. The row was in this database, the audit entry was in
  this database, and the application log records only that a callback arrived,
  never whose. There is no source to enumerate from. Instead:
    1. Treat every user account connected during that window as
       un-erased, and run the product's purge again for each, or ask the
       person to submit the deletion request again.
    2. Watch the product's privacy contact address. A confirmation code that
       has stopped resolving is someone whose erasure this restore undid.
  Their old confirmation codes will NOT work again even after you re-run the
  purge: the code was stored on the record that went, and running the purge
  writes no new record - only a new data-deletion request creates one. So
  the code cannot be reissued either. Answer those people in writing.
  ${consequence}
NOTICE
