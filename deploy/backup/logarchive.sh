#!/bin/sh
# Nightly encrypted archive of the containers' logs, kept for
# LOG_ARCHIVE_KEEP_DAYS (default 420). Runs as a
# long-lived compose service (see compose.example.yml) beside backup.sh, from
# the same image, under the same passphrase and R2 bucket.
#
# Why: docker's json-file driver keeps at most five 10 MB files per container
# (compose.example.yml, x-log-bounds), which is days of history for a busy
# container, while incident regimes assume 180 days
# (CERT-In) to a year (DPDP). Each night this copies every log file the
# stack's containers wrote to in the last day, tars them (read in place, through
# a staging directory of symlinks) under the container's name, encrypts the
# archive exactly as backup.sh encrypts a dump, keeps it
# locally for LOG_ARCHIVE_KEEP_DAYS (with LOG_ARCHIVE_PRUNE=off, until something
# else takes it) and PUTs a copy under logs/ in the R2 bucket. A line that
# stays in the active file across several nights appears
# in several archives; that overlap is cheaper than tracking offsets. With the
# argument `once` it archives one time and exits (see the bottom).
#
# pipefail is load-bearing for the same reason as in backup.sh: without it a
# failing tar still exits 0 through gzip|openssl and an empty archive would be
# promoted as a good night.
set -eu
set -o pipefail

command -v openssl >/dev/null 2>&1 || {
    echo "openssl is missing from this image; log archives cannot be encrypted" >&2
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

# 420 days: at least the year DPDP asks for, and with the nightly prune and
# the R2 sweep's day of lag still inside "up to 14 months" (the shortest 14
# months is 424 days). With LOG_ARCHIVE_PRUNE=off that promise is the mover's:
# archives stay here, past this window, until it takes them.
KEEP_DAYS="${LOG_ARCHIVE_KEEP_DAYS:-420}"
# Empty or on: the nightly prune runs. off: it is skipped (see the loop below).
PRUNE="${LOG_ARCHIVE_PRUNE:-on}"
case "$PRUNE" in
    on|off) ;;
    *) echo "LOG_ARCHIVE_PRUNE must be empty, on or off" >&2; exit 1 ;;
esac
# /log-archive in the container; overridable so the script can run under a test.
ROOT="${LOG_ARCHIVE_ROOT:-/log-archive}"
# Where a night's file list is staged: a directory of symlinks on a tmpfs
# private to this container, so the plaintext logs are never copied anywhere
# and nothing under ROOT (which the operator rsyncs off the host) is ever
# unencrypted.
STAGE="${LOG_ARCHIVE_STAGE:-/stage}"
# docker's container directories, mounted read-only: <id>/config.v2.json names
# the container, <id>/<id>-json.log and its rotated .1 .. .4 are the logs.
LOGS="${DOCKER_LOGS:-/docker-logs}"
# Only containers of this compose project are archived; the deployer starts
# the stack with `-p "$COMPOSE_PROJECT"`, so the names are <project>-api-1 and
# so on. Other projects on the same daemon (a verify run, an experiment) are
# not ours.
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT must be set (product.settings)}"
PROJECT="${LOG_ARCHIVE_PROJECT:-$COMPOSE_PROJECT}"
# A file counts as written to in the last day if its mtime is within this
# many minutes: 25 hours, so the drift of a nightly loop can never open a gap
# between two archives. Rotation renames a file and keeps its mtime.
RECENT_MINUTES=1500

. "$(dirname "$0")/crypto.sh"

# Off-site copy under the logs/ prefix of the same bucket backup.sh uses, so
# one lifecycle rule per prefix sets each kind's expiry.
# Same shape as backup.sh: curl signs the request, the credentials travel by
# stdin config, a failed upload is logged and the local archive stands.
R2_ENDPOINT="${BACKUP_R2_ENDPOINT:-}"
R2_BUCKET="${BACKUP_R2_BUCKET:-}"
offsite() {
    [ -n "$R2_ENDPOINT" ] || return 0
    for f in "$@"; do
        if printf 'user = "%s:%s"\n' "${BACKUP_R2_ACCESS_KEY_ID:-}" "${BACKUP_R2_SECRET_ACCESS_KEY:-}" \
            | curl -fsS --max-time 300 --proto '=https' -K - --aws-sigv4 "aws:amz:auto:s3" -T "$f" -o /dev/null \
                "${R2_ENDPOINT}/${R2_BUCKET}/logs/$(basename "$f")"; then
            echo "off-site copy written: logs/$(basename "$f")"
        else
            echo "off-site copy FAILED: logs/$(basename "$f")" >&2
        fi
    done
}
# The same startup checks as backup.sh, for the same reasons: say at startup
# whether uploads will work, and never splice a newline into the curl config.
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
    echo "off-site copies on: bucket ${R2_BUCKET}, prefix logs/"
else
    echo "off-site copies off"
fi

# The container's name from its config.v2.json, without jq: the file is one
# line of JSON, and the top-level Name is the only value written with a
# leading slash ("/<project>-api-1"); the nested Name keys under MountPoints
# hold volume names or nothing.
container_name() {
    tr ',' '\n' <"$1" | sed -n 's/^{*"Name":"\/\([^"]*\)".*/\1/p' | head -n 1
}

archive() {
    ts="$(date -u +%Y%m%d-%H%M%S)"
    stage="${STAGE}/stage-${ts}"
    # Not -p: the directory is the lock. Two archives in the same second
    # (the nightly pass and a pre-deploy `once`) would otherwise share it and
    # write the same output file over each other.
    mkdir -p "$STAGE"
    mkdir "$stage" || { echo "another archive started at ${ts}; skipping" >&2; return 1; }
    copied=0
    for dir in "$LOGS"/*/; do
        [ -f "${dir}config.v2.json" ] || continue
        name="$(container_name "${dir}config.v2.json")"
        case "$name" in
            "${PROJECT}"-*) ;;
            *) continue ;;
        esac
        # A name becomes a directory below; docker allows [A-Za-z0-9_.-] only,
        # so anything else is a file we should not be reading.
        case "$name" in
            *[!A-Za-z0-9_.-]*) echo "skipping a container with an unexpected name" >&2; continue ;;
        esac
        for f in "${dir}"*-json.log*; do
            [ -f "$f" ] || continue
            [ -n "$(find "$f" -mmin -"$RECENT_MINUTES")" ] || continue
            mkdir -p "${stage}/${name}"
            ln -s "$f" "${stage}/${name}/"
            copied=$((copied + 1))
        done
    done
    if [ "$copied" -eq 0 ]; then
        echo "no container log written to in the last day; nothing archived at ${ts}"
        rm -rf "$stage"
        return 0
    fi
    out="${ROOT}/logs-${ts}.tar.gz.enc"
    # Not .part: the backup container mounts the parent directory and clears
    # every *.part at the start of its own loop.
    tmp="${out}.writing"
    # -h: the staged entries are symlinks, tar reads the log files through
    # them and stores them under the container's name.
    if tar -chf - -C "$stage" . \
        | gzip \
        | backup_encrypt "$tmp" \
        && mv "$tmp.hmac" "${out}.hmac" \
        && mv "$tmp" "$out"; then
        echo "log archive written: $out (${copied} files, $(wc -c <"$out") bytes)"
        offsite "$out" "${out}.hmac"
    else
        rm -f "$tmp" "$tmp.hmac" "${out}.hmac"
        echo "log archive FAILED at ${ts}" >&2
        rm -rf "$stage"
        return 1
    fi
    rm -rf "$stage"
}

# `once`: one archive now, then exit with its result. The deployer runs this
# inside the live container just before compose recreates anything, because
# docker deletes a removed container's log files with it and the nightly
# pass would miss everything written since the previous night. Nothing is
# cleaned or pruned here: the nightly process owns that, and its staging
# directory may be in use.
if [ "${1:-}" = once ]; then
    archive
    exit 0
fi

# A container killed mid-archive leaves a partial file behind (and, under a
# test, a staging directory); clear both once, at start.
# Crypto leftovers share .writing's startup-only cleanup and accepted once overlap.
rm -rf "${STAGE}"/stage-* "${ROOT}"/*.writing "${ROOT}"/*.writing.hmac "${ROOT}"/.backup-crypto.*
while true; do
    # The retention window, applied whatever tonight's archive does: an
    # archive that fails must not keep old ones alive past their day.
    # LOG_ARCHIVE_PRUNE=off leaves retention to whatever takes the archives, and
    # the disk then becomes the limit.
    [ "$PRUNE" = off ] || find "$ROOT" -name 'logs-*.tar.gz.enc*' -mtime +"$KEEP_DAYS" -delete
    # A failed night is reported inside; the loop goes on to the next.
    archive || :
    # Nightly from container start (plus the archive-on-start above).
    sleep 86400
done
