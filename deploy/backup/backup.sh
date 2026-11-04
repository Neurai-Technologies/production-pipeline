#!/bin/sh
# Daily encrypted Postgres dumps with retention. Runs as a long-lived compose
# service (see compose.example.yml); writes to /backups, which is bind-mounted
# to BACKUP_DIR on the host so the operator can rsync it off-box.
#
# With the argument `once` it runs one cycle and exits 0 on success, non-zero
# on failure (see the bottom). That is for the deployer's PRE_DEPLOY_HOOK:
#   docker compose -p <project> --project-directory <dir> run --rm -T backup once
# so the backup taken before a deploy gets the same encryption, naming and
# retention as the scheduled ones.
#
# pipefail is load-bearing: without it a failing pg_dump still exits 0 through
# gzip|openssl, the empty file gets promoted as a "successful" backup, and
# retention then ages the real history out night by night.
set -eu
set -o pipefail

case "${1:-}" in
    ""|once) ;;
    *) echo "usage: backup.sh [once]" >&2; exit 2 ;;
esac

command -v openssl >/dev/null 2>&1 || {
    echo "openssl is missing from this image; backups cannot be encrypted" >&2
    exit 1
}
# lock() below cannot tell a missing flock from a busy lock (busybox flock -n
# exits 1 for both contention and any other error), so without this every
# cycle would be skipped as "held by another cycle".
command -v flock >/dev/null 2>&1 || {
    echo "flock is missing from this image; backup cycles cannot be serialised" >&2
    exit 1
}
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set}"
# The label-only salt the .hmac sidecars are keyed under; restore.sh reads
# the same key. Required, with no default: changing it invalidates the
# sidecars of every existing dump and archive (restore refuses them), so an
# existing deployment keeps the value its dumps were made with.
: "${BACKUP_HMAC_SALT:?BACKUP_HMAC_SALT must be set (product.settings)}"
# openssl enc -S keeps only the first 8 bytes of a salt, so a longer one
# would be cut silently and two salts sharing a prefix would give one key.
[ "$(printf %s "$BACKUP_HMAC_SALT" | wc -c | tr -d ' ')" -eq 8 ] || {
    echo "BACKUP_HMAC_SALT must be exactly 8 bytes (openssl uses only 8)" >&2
    exit 1
}
# The product's settings (env_file: product.settings).
: "${DB_SERVICE:?DB_SERVICE must be set (product.settings)}"
: "${DB_NAME:?DB_NAME must be set (product.settings)}"
: "${DB_BACKUP_USER:?DB_BACKUP_USER must be set (product.settings)}"
: "${BACKUP_PREFIX:?BACKUP_PREFIX must be set (product.settings)}"
# The prefix goes into the prunes' find -name patterns, where a * or ? would
# widen what they delete: letters, digits, _ and - only.
case "$BACKUP_PREFIX" in
    *[!A-Za-z0-9_-]*) echo "BACKUP_PREFIX may hold only letters, digits, _ and -" >&2; exit 1 ;;
esac

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
# The ceiling, enforced whatever the dumps do. See the two prunes in cycle():
# KEEP_DAYS is the ordinary window and only applies after a good write, which
# leaves the published promise unkept exactly when dumps are failing. This one
# always runs. It sits under the window /privacy publishes (60 days), and the
# distance between the two is the time a broken chain has to be noticed in.
MAX_DAYS="${BACKUP_MAX_DAYS:-55}"
# /backups in the container; overridable so the script can run under a test.
ROOT="${BACKUP_ROOT:-/backups}"
# Touched after a dump has passed the size and HMAC checks below, on a small
# volume the worker mounts read-only. The worker's daily pass reads its age and
# raises the alarm when backups have stopped. The dumps themselves stay in a
# root-only directory the worker cannot list, which is how it should be.
HEARTBEAT="${BACKUP_HEARTBEAT:-/heartbeat/last-success}"

if [ "$MAX_DAYS" -lt "$KEEP_DAYS" ]; then
    echo "BACKUP_MAX_DAYS ($MAX_DAYS) is below BACKUP_KEEP_DAYS ($KEEP_DAYS); dumps will be deleted at the lower figure" >&2
fi

. "$(dirname "$0")/crypto.sh"

# Off-site copy: every dump and its sidecar are also PUT to an S3 bucket
# (Cloudflare R2) when BACKUP_R2_ENDPOINT is set. curl signs the request
# itself; the credentials travel by stdin config, never argv. Best effort:
# the local dump is already safe, so a failed upload is logged, not fatal.
R2_ENDPOINT="${BACKUP_R2_ENDPOINT:-}"
R2_BUCKET="${BACKUP_R2_BUCKET:-}"
offsite() {
    [ -n "$R2_ENDPOINT" ] || return 0
    for f in "$@"; do
        if printf 'user = "%s:%s"\n' "${BACKUP_R2_ACCESS_KEY_ID:-}" "${BACKUP_R2_SECRET_ACCESS_KEY:-}" \
            | curl -fsS --max-time 300 --proto '=https' -K - --aws-sigv4 "aws:amz:auto:s3" -T "$f" -o /dev/null \
                "${R2_ENDPOINT}/${R2_BUCKET}/$(basename "$f")"; then
            echo "off-site copy written: $(basename "$f")"
        else
            echo "off-site copy FAILED: $(basename "$f")" >&2
        fi
    done
}
# Say at startup whether uploads will work, not only when the first one fails.
# The credentials are spliced into a curl config line, where a newline would
# start a second directive; R2 keys never need anything outside this set.
if [ -n "$R2_ENDPOINT" ]; then
    case "$R2_ENDPOINT" in
        https://*) ;;
        *) echo "off-site copies MISCONFIGURED: BACKUP_R2_ENDPOINT must start with https://" >&2; R2_ENDPOINT="" ;;
    esac
fi
if [ -n "$R2_ENDPOINT" ] && { [ -z "$R2_BUCKET" ] || [ -z "${BACKUP_R2_ACCESS_KEY_ID:-}" ] || [ -z "${BACKUP_R2_SECRET_ACCESS_KEY:-}" ]; }; then
    echo "off-site copies MISCONFIGURED: bucket, access key id or secret is empty" >&2
    R2_ENDPOINT=""
fi
case "${BACKUP_R2_ACCESS_KEY_ID:-}:${BACKUP_R2_SECRET_ACCESS_KEY:-}" in
    *[!A-Za-z0-9+/=_:-]*) echo "off-site copies MISCONFIGURED: credentials contain unexpected characters" >&2; R2_ENDPOINT="" ;;
esac
if [ -n "$R2_ENDPOINT" ]; then
    echo "off-site copies on: bucket ${R2_BUCKET}"
else
    echo "off-site copies off"
fi

# One cycle at a time on this backup directory: the scheduled service and a
# pre-deploy `once` share it, and two cycles in the same second would write
# the same .part. The lock is fd 9 on /lock/backup.lock, the backup-lock
# named volume: a docker volume inside the VM, where flock works (/backups is
# a host folder bind-mounted into the VM, where it is unproven), mounted by
# this service alone, so no other container can hold the lock and stall the
# cycles. `compose run backup once` mounts the same service volumes, so both
# runs lock one file. The kernel drops the lock when the process dies.
# busybox flock has no -w, so the wait is bounded here: 60 tries, 5 s apart
# (59 waits, about 5 minutes), so a pre-deploy `once` still has time to dump
# inside the deployer's hook limit.
# Overridable only so the script can run under a test. Refused, never moved
# elsewhere: a lock in any other folder is one the other run does not see.
LOCK_DIR="${BACKUP_LOCK_DIR:-/lock}"
if [ ! -d "$LOCK_DIR" ] || [ ! -w "$LOCK_DIR" ]; then
    echo "backup: $LOCK_DIR missing or not writable (the cycle lock lives there); add backup-lock:/lock to the backup service's volumes and declare backup-lock: under the top-level volumes" >&2
    exit 1
fi
exec 9>>"${LOCK_DIR}/backup.lock"
lock() {
    tries=0
    until flock -n 9; do
        tries=$((tries + 1))
        if [ "$tries" -ge 60 ]; then
            echo "backup lock held by another cycle for about 5 minutes; this cycle skipped" >&2
            return 1
        fi
        sleep 5
    done
}

# One cycle, run under the lock: sweep, prune at the ceiling, dump, check,
# prune at the window. A failed dump is reported here and leaves
# result=failed; anything else that fails still stops the script through
# set -e.
cycle() {
    result=ok
    # A container killed mid-write leaves a .part behind. Under the lock, so
    # no .part here belongs to a running cycle.
    find "$ROOT" \( -name '*.part' -o -name '*.part.hmac' \) -delete
    rm -rf "$ROOT"/.backup-crypto.*
    # The ceiling, before the dump is attempted and whatever it does. Retention
    # used to live entirely in the success branch below, so a database too
    # broken to dump was also a database whose old dumps stopped expiring - and
    # personal data outliving the window /privacy publishes is a promise broken
    # in the one case nobody is watching. Deliberately a second, longer window
    # rather than a move of the first: the reason the prune sits in the success
    # branch is still true, and a broken database must not age the good history
    # out from under the operator at fourteen days.
    find "$ROOT" -name "${BACKUP_PREFIX}-*.sql.gz.enc*" -mtime +"$MAX_DAYS" -delete
    ts="$(date -u +%Y%m%d-%H%M%S)"
    out="${ROOT}/${BACKUP_PREFIX}-${ts}.sql.gz.enc"
    tmp="${out}.part"
    # -pbkdf2 so the passphrase is stretched; env: keeps it out of argv.
    # DB_BACKUP_USER is a read-only role: this container never holds the owner
    # credential, so compromising it yields SELECT, not write/DDL.
    if pg_dump -h "$DB_SERVICE" -U "$DB_BACKUP_USER" "$DB_NAME" \
        | gzip \
        | backup_encrypt "$tmp" "$(basename "$out")" \
        && [ "$(wc -c <"$tmp")" -gt 1024 ] \
        && mv "$tmp.hmac" "${out}.hmac"; then
        mv "$tmp" "$out"
        echo "backup written: $out ($(wc -c <"$out") bytes)"
        # The dump is safe on disk at this point; a heartbeat that cannot be
        # written is worth a line, not a failed backup.
        date -u +%Y-%m-%dT%H:%M:%SZ >"$HEARTBEAT" 2>/dev/null \
            || echo "heartbeat not written: $HEARTBEAT (the alarm will think backups stopped)" >&2
        offsite "$out" "${out}.hmac"
        # Retention prunes only after a successful, plausibly-sized write: a
        # broken database must never age the good history out from under the
        # operator.
        find "$ROOT" -name "${BACKUP_PREFIX}-*.sql.gz.enc*" -mtime +"$KEEP_DAYS" -delete
    else
        rm -f "$tmp" "$tmp.hmac" "${out}.hmac"
        echo "backup FAILED at ${ts}" >&2
        result=failed
    fi
}

# `once`: one cycle now, then exit with its result.
if [ "${1:-}" = once ]; then
    lock || exit 1
    cycle
    [ "$result" = ok ] || exit 1
    exit 0
fi

while true; do
    if lock; then
        cycle
        flock -u 9
    fi
    # Every 6h from container start (the first cycle runs at start): the dumps
    # are the only disaster path, and 2026-09-01 proved the loss window is
    # whatever this interval is. A restart just moves the window.
    sleep 21600
done
