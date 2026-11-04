#!/bin/bash
# Bring the product's VM back after a Mac restart - and, when the database
# volume is provably gone, restore it from the newest encrypted dump.
#
# Installed as a LaunchAgent (launchagent.plist.template, rendered by
# install-autostart.sh), so it
# runs at login with launchd's minimal environment and must assume nothing
# from the shell profile.
#
# The 2026-09-01 incident shaped every guard here. Two starters raced (a
# manual colima restart and a second boot), the VM came back on an empty data
# disk, and for half an hour "the data is gone" was indistinguishable from
# "the data is shadowed under a mount". Hence: a single-instance lock so this
# script never races ITSELF (a manual colima command takes no lock - the rule
# for humans stays: never cycle the VM while this agent is loaded); a
# data-disk verification so
# an unmounted datadisk (docker quietly running on the root disk) is treated
# as "do not touch, alert" rather than "empty, restore over it"; and the
# restore itself only fires when the disk is verified AND the volume is
# genuinely absent AND the dump is fresh.
#
# A dump's order and age come from its file NAME, never from file times: those
# are not authenticated, so touching an older genuine dump on a removable disk
# would select it and pass the age cap. Only a v2 sidecar binds the name into
# the HMAC, and restore.sh refuses a renamed dump, so auto-restore uses only
# dumps with one; a legacy dump is restored by hand with restore.sh.
#
# The restore path executes LIBRARY_DIR's restore.sh and COMPOSE_DIR's
# docker-compose.yml from the repo working tree at login. That is a
# deliberate, operator-accepted exception to the installed-copy rule the
# plist documents: unattended recovery cannot work from a frozen copy of a
# stack definition that deploys from git.
set -eu
# Settings: the product.settings install-autostart.sh copied beside this script.
# shellcheck source=/dev/null
. "$(dirname "$0")/product.settings"
: "${PRODUCT:?}"
: "${REPO_DIR:?}"
: "${COLIMA_PROFILE:?}"
: "${COLIMA_HOME:?}"
: "${APP_SUPPORT_DIR:?}"
: "${COMPOSE_PROJECT:?}"
: "${COMPOSE_DIR:?}"
: "${DB_NAME:?}"
: "${DB_USER:?}"
: "${LIBRARY_DIR:?}"
: "${BACKUP_PREFIX:?}"
: "${RESTORE_VERIFY_SQL:?}"
# Optional settings and their defaults. ERASURE_CHECK_SQL empty skips the
# erasure check and its alert after an auto-restore.
DATA_DISK_DEVICE="${DATA_DISK_DEVICE:-/dev/vdb1}"
DEPLOYER_SERVICE="${DEPLOYER_SERVICE:-deployer}"
: "${DB_SERVICE:?DB_SERVICE must be set (product.settings)}"
OPS_PROFILE="${OPS_PROFILE:-ops}"
ERASURE_CHECK_SQL="${ERASURE_CHECK_SQL:-}"
RESTORE_WORK_DIR="${RESTORE_WORK_DIR:-}"

# launchd gives us /usr/bin:/bin:/usr/sbin:/sbin. colima shells out to limactl,
# which lives in /opt/homebrew/bin, so without this colima dies with
# "dependency check failed for VM: lima not found" and the VM never starts.
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
# The VM lives in its OWN home, outside the product tree so that neither
# `git clean` nor an agent working in the repo can reach it: state, disks and
# socket paths are self-contained and collide with no other VM, present or
# future. NOT isolated (host-global regardless of home): the 127.0.0.1 port
# namespace (tunnel-only ingress makes that collision-only) and the docker
# CONTEXT name colima-<profile> - which is why this script always sets
# DOCKER_HOST explicitly instead of trusting the context.
export COLIMA_HOME
# colima honours LIMA_HOME OVER its own COLIMA_HOME. A stray LIMA_HOME export
# in a shell is exactly how the 2026-09-01 shadow-VM incident happened: a
# second, empty VM of this product provisioned in another project's tree, binding
# the shared socket out from under production. launchd never sources .zshrc,
# but this script must be safe to run from ANY shell.
unset LIMA_HOME

# Keep COLIMA_PROFILE short: macOS caps unix socket paths at 104 chars, and
# <home>/_lima/colima-<profile>/ssh.sock.<16-char suffix> goes over it with
# a long name. A first provisioning attempt died on exactly that.
PROFILE=$COLIMA_PROFILE
REPO=$REPO_DIR
BK="${BACKUP_DIR:-${REPO}/backups}"
SOCKET="${COLIMA_HOME}/${PROFILE}/docker.sock"
# The existing VM's config. Waiting on THIS rather than on a directory is what
# enforces "start the VM that exists, never create a new one": COLIMA_HOME is
# on an external volume, and a bare directory test passes on a leftover mount
# point. If this file is absent the VM state is not there either, and starting
# colima would build a fresh, empty one - silently discarding pgdata. That is
# how the database was lost on 2026-08-31.
VM_STATE="${COLIMA_HOME}/_lima/colima-${PROFILE}/lima.yaml"
LOG="${HOME}/Library/Logs/${PRODUCT}-vm-autostart.log"
LOCK="${APP_SUPPORT_DIR}/vm-autostart.lock"
# Left behind by a failed auto-restore whose volume could not be removed, so
# the next login refuses to hand that volume to the deployer. A later restore
# that succeeds clears it; otherwise a human removes it with the volume.
FAILED_MARK="${APP_SUPPORT_DIR}/restore-failed"
# Auto-restore only over a dump younger than this; an older one means
# something has been wrong for days and a human should decide.
MAX_DUMP_AGE_HOURS="${MAX_DUMP_AGE_HOURS:-48}"
# The database volume compose creates for the stack's postgres service.
: "${DB_VOLUME:?DB_VOLUME must be set (product.settings)}"
PGDATA_VOLUME="${COMPOSE_PROJECT}_${DB_VOLUME}"

mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK")"
exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') autostart ==="

alert() {
    # Both channels on purpose: the log for the record, the notification so a
    # failure at login is seen today, not found next week. The message goes
    # through argv, never interpolated into the AppleScript source - device
    # names and dump filenames are data, and a quote in them must not become
    # code running as the logged-in user.
    echo "ALERT: $*"
    timeout -k 10 10 osascript -e 'on run {msg, ttl}' \
        -e 'display notification msg with title ttl' \
        -e 'end run' -- "$*" "${PRODUCT} VM" 9>&- 2>/dev/null || true
}

fail() {
    alert "$*"
    exit 1
}

# Prints what is wrong with RESTORE_WORK_DIR; nothing means it is private. A
# path ending in "/" or "/." is reported first: the -L test would resolve a
# symlink written that way and pass it.
restore_work_dir_problem() {
    case "$RESTORE_WORK_DIR" in
        */ | */. | */..) echo "RESTORE_WORK_DIR '${RESTORE_WORK_DIR}' has a trailing slash or dot - name the folder itself" ;;
        /*)
            if [ -L "$RESTORE_WORK_DIR" ]; then
                echo "RESTORE_WORK_DIR ${RESTORE_WORK_DIR} is a symlink"
            elif [ ! -d "$RESTORE_WORK_DIR" ]; then
                echo "RESTORE_WORK_DIR ${RESTORE_WORK_DIR} is missing or not a directory"
            elif [ ! -O "$RESTORE_WORK_DIR" ]; then
                echo "RESTORE_WORK_DIR ${RESTORE_WORK_DIR} is not owned by the current user"
            elif [ -z "$(find "$RESTORE_WORK_DIR" -maxdepth 0 -perm 700)" ]; then
                echo "RESTORE_WORK_DIR ${RESTORE_WORK_DIR} is not exactly mode 700"
            # An ACL can open a mode 700 folder to everyone. macOS `ls -lde` prints
            # one line per ACL entry after the folder's own line; plain `ls -ld`
            # cannot tell, as its @ for an xattr hides the +. A failed ls prints
            # nothing, which also counts as not private.
            elif [ "$(ls -lde "$RESTORE_WORK_DIR" | wc -l)" -ne 1 ]; then
                echo "RESTORE_WORK_DIR ${RESTORE_WORK_DIR} has an ACL or could not be checked for one"
            fi
            ;;
        *) echo "RESTORE_WORK_DIR '${RESTORE_WORK_DIR}' is not an absolute path" ;;
    esac
}

# Removes what a restore left in RESTORE_WORK_DIR. The folder is this product's
# auto-restore's alone. The lock does not prove no restore is running: restore.sh
# runs with 9>&-, so an orphan does not hold it. An orphan whose files are swept
# before psql starts refuses or errors; once psql is reading, it finishes; never
# a partial restore. An absolute path with nothing there stays silent: an absent
# disk at a normal login must not alert or stop the VM. Any other bad folder
# alerts and nothing is removed from it; step 6 refuses it if a restore needs it.
# A file that will not go alerts too. Either way the run goes on, because neither
# must stop the VM on every login.
sweep_restore_work_dir() {
    local problem
    [ -n "$RESTORE_WORK_DIR" ] || return 0
    case "$RESTORE_WORK_DIR" in
        */ | */. | */..) ;;
        /*) [ -e "$RESTORE_WORK_DIR" ] || [ -L "$RESTORE_WORK_DIR" ] || return 0 ;;
    esac
    problem=$(restore_work_dir_problem)
    if [ -n "$problem" ]; then
        alert "${problem} - nothing removed from it, and a restore would refuse"
        return 0
    fi
    rm -rf "$RESTORE_WORK_DIR"/"$PRODUCT"-restore.* "$RESTORE_WORK_DIR"/backup-crypto.* \
        || alert "could not remove leftovers of an earlier restore from RESTORE_WORK_DIR ${RESTORE_WORK_DIR} - they may hold plaintext; remove them by hand"
}

# 0. One starter at a time. The lock also stays held for the whole run, so a
#    second login event (or an impatient re-kick) leaves. flock's absence must
#    be its own loud failure: a missing binary returns 127 into the same ||,
#    which would otherwise read as "lock held" and silently no-op every login.
command -v flock >/dev/null || fail "flock missing (brew install flock) - refusing to run unlocked"
command -v timeout >/dev/null || fail "timeout missing (brew install coreutils) - refusing to run unbounded"
# The rule for every colima, docker and osascript call below, and restore.sh:
# it closes descriptor 9 (9>&-) and runs under `timeout -k 10 N`. Plain
# `timeout N` only sends SIGTERM and then waits: a docker CLI forwards it to
# the container and waits too, so `timeout 3 docker run ... sleep 60` ran 60s.
# -k 10 sends SIGKILL 10s after the SIGTERM, for a child that ignores it or
# merely forwards it, so every limit is a hard bound. A child that hangs
# or daemonizes (colima's limactl, its usernet) must not keep the lock after
# this script exits - the next login would read "lock held" and leave, and
# the VM would never start (2026-10-04 here, 2026-10-20 on a sibling VM).
exec 9>"$LOCK"
flock -n 9 || { echo "another autostart run holds the lock; leaving"; exit 0; }

# SIGKILL or a power loss skips a restore's own cleanup, and after a power loss the volume
# survives, so step 6 may never run again: every run sweeps, not only a restore.
sweep_restore_work_dir

# 1. The volume, proven by the VM's own config file.
for _ in $(seq 1 60); do
    [ -f "$VM_STATE" ] && break
    sleep 5
done
[ -f "$VM_STATE" ] \
    || fail "no VM config at ${VM_STATE} after 5 minutes - external volume probably not mounted; refusing to run colima (it would create an empty VM)"
echo "VM state present: ${VM_STATE}"

# 2. The VM. Idempotent - a running profile is left alone.
rc=0
timeout -k 10 10 colima status "$PROFILE" 9>&- >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "colima profile '${PROFILE}' already running"
else
    # A status that did not answer is not "not running", and the log must
    # not read as if it were. Going on to start is still right: colima start
    # on a VM that is already up says "already running, ignoring" and returns
    # at once (measured 2026-10-23), and on a wedged one fails within 300s.
    [ "$rc" -ne 124 ] && [ "$rc" -ne 137 ] \
        || echo "colima status did not answer in 10s or was killed (exit $rc); trying start"
    echo "starting colima profile '${PROFILE}'"
    # colima leaves daemons behind (its own, and lima's hostagent and
    # usernet). On 2026-10-04 the volume dropped, the VM died, those daemons
    # held the inherited lock, and production stayed down with the volume
    # back until the leftovers were killed by hand.
    timeout -k 10 300 colima start --profile "$PROFILE" 9>&- || fail "colima start failed or ran past 300s (exit $?) - leaving"
fi

# 3. Readiness. The socket FILE existing proves nothing - ask the daemon.
export DOCKER_HOST="unix://${SOCKET}"
for _ in $(seq 1 60); do
    timeout -k 10 10 docker version 9>&- >/dev/null 2>&1 && break
    sleep 5
done
timeout -k 10 10 docker version 9>&- >/dev/null 2>&1 || fail "docker daemon never answered on ${SOCKET}"
echo "docker daemon ready"

# 4. The data disk, before believing anything docker says. If the additional
#    datadisk missed its mount, docker runs happily on the ROOT disk and
#    reports no volumes - which looks exactly like data loss while the real
#    data sits one failed mount away. DATA_DISK_DEVICE is this profile's datadisk;
#    if the disk layout ever changes, change it here too.
# findmnt renders a bind mount of a subdirectory as device[/subpath] -
# /var/lib/docker is bound from the datadisk's /docker subdir, so the healthy
# answer is "/dev/vdb1[/docker]", not bare "/dev/vdb1" (the first live run
# aborted on exactly that). The case below owns the accept/reject policy.
# A timeout is reported as one: "on 'none'" would send a human hunting for
# an unmounted disk when the VM simply did not answer.
rc=0
DOCKER_DEV=$(timeout -k 10 20 colima ssh --profile "$PROFILE" -- findmnt -n -o SOURCE /var/lib/docker 9>&- 2>/dev/null) || rc=$?
# timeout exits 124 when SIGTERM ended the child and 137 when SIGKILL had to,
# or when something else (the OOM killer) killed it; 137 alone cannot tell.
[ "$rc" -ne 124 ] && [ "$rc" -ne 137 ] \
    || fail "data-disk check (colima ssh findmnt) timed out after 20s or was killed (exit $rc) - cannot tell where docker data is; refusing to go on"
[ "$rc" -eq 0 ] || DOCKER_DEV=none
# Pinned to the exact healthy renderings, not a vdb1 prefix: a wrong
# SUBPATH of the right device ("/dev/vdb1[/wrong]") is docker on an empty
# corner of the correct disk - zero volumes, step 5 reads "gone", step 6
# restores, and the real database sits stranded one directory away. The
# same shadow ambiguity this gate exists for, one level in.
case "$DOCKER_DEV" in
    "$DATA_DISK_DEVICE" | "${DATA_DISK_DEVICE}[/docker]") : ;;
    *) fail "docker data is on '${DOCKER_DEV}', not the datadisk ${DATA_DISK_DEVICE}[/docker] - NOT safe to trust or restore; investigate before anything writes" ;;
esac
echo "docker data confirmed on the datadisk"

# 5. The database volume. Present: the stack brings itself back
#    (restart: unless-stopped) and the deployer resumes; just report.
#
# Captured and then tested, never piped into the condition. Wrong in the safe
# direction this costs a restart; wrong in the other it runs step 6 against a
# live database, and step 6's cleanup deletes the volume. A read that failed
# is not an answer, so it refuses here rather than guessing absence - the
# stack restarts itself via `restart: unless-stopped`, so refusing is cheap.
# The 2>&1 is for the ALERT, not the log: the script already sends everything
# there, but osascript shows only what fail() is handed.
# The two deployer reads below are left piped on purpose: they sit inside the
# volume-present branch and a false reading there raises an alert, which is
# the direction that costs nothing.
pgdata_present() {
    local volumes rc
    volumes=$(timeout -k 10 10 docker volume ls --format '{{.Name}}' 9>&- 2>&1) || {
        rc=$?
        [ "$rc" -ne 124 ] && [ "$rc" -ne 137 ] \
            || fail "docker volume ls timed out after 10s or was killed (exit $rc) - refusing to decide whether the database exists"
        fail "could not list docker volumes (${volumes}) - refusing to decide whether the database exists"
    }
    printf '%s\n' "$volumes" | grep -qx "$PGDATA_VOLUME"
}

if pgdata_present; then
    [ ! -e "$FAILED_MARK" ] \
        || fail "a failed auto-restore left its volume ${PGDATA_VOLUME} behind (${FAILED_MARK}) - do not start the deployer; remove the volume and the marker so the next login restores again, or only the marker once you have restored by hand and verified the volume"
    for _ in $(seq 1 12); do
        timeout -k 10 10 docker ps --filter 'status=running' --format '{{.Names}}' 9>&- | grep -q "$DEPLOYER_SERVICE" && break
        sleep 5
    done
    if timeout -k 10 10 docker ps --filter 'status=running' --format '{{.Names}}' 9>&- | grep -q "$DEPLOYER_SERVICE"; then
        echo "deployer is running; it will pull the newest green commit within ~90s"
    else
        alert "VM and data are up but the deployer did not come back - start it manually (see README)"
    fi
    echo "running containers: $(timeout -k 10 10 docker ps --format '{{.Names}}' 9>&- | tr '\n' ' ')"
    exit 0
fi

# 6. Guarded auto-restore: datadisk verified (step 4), volume genuinely
#    absent, dump fresh. All three or nothing.
#
# Fresh is read from the UTC date backup.sh puts in the dump's name. Nothing
# here trusts a file time. Nothing here trusts the name or the sidecar's marker
# either, until restore.sh has verified the HMAC that binds them: a name or
# marker forged on the dump actually chosen makes it refuse, and the run then
# fails hard, with no fall back to an older dump. A dump skipped or passed over
# at the choice below leaves the next older v2 dump, as deleting it would, and
# only the log records it. Renaming the newest dump to an older date, or out of
# the pattern, leaves the next older one chosen with no log line at all. What
# this cannot see is a newer dump deleted from the disk; the older one left is
# then the newest. The age cap bounds all of these.
#
# A skip logs safe_name, so a file name cannot forge a log line. It comes from tr
# under LC_ALL=C: bash 3.2 reads a bracket range by the locale's collation, which
# lets a letter outside ASCII through in a UTF-8 locale.
echo "${PGDATA_VOLUME} is absent on a verified datadisk - entering auto-restore"
# Sets newest to the v2 dump with the greatest name date, newest_epoch to that
# date, and not_v2 to how many dumps were passed over for their sidecar.
pick_newest_dump() {
    local dump name safe_name stamp epoch now
    now=$(date +%s)
    newest=""
    newest_epoch=0
    not_v2=0
    for dump in "$BK/${BACKUP_PREFIX}"-*.sql.gz.enc; do
        # A pattern that matched nothing stays as its own text.
        [ -e "$dump" ] || [ -L "$dump" ] || continue
        name=$(basename "$dump")
        safe_name=$(printf %s "$name" | LC_ALL=C tr -c 'A-Za-z0-9._-' '?')
        stamp=${name#"${BACKUP_PREFIX}"-}
        stamp=${stamp%.sql.gz.enc}
        case "$stamp" in
            [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
            *) echo "skipping ${safe_name}: its name holds no YYYYMMDD-HHMMSS date"; continue ;;
        esac
        # date carries 20260230 over into March, so only the round trip rejects it.
        epoch=$(date -j -u -f '%Y%m%d-%H%M%S' "$stamp" +%s 2>/dev/null) || epoch=""
        if [ -z "$epoch" ] || [ "$(date -j -u -r "$epoch" '+%Y%m%d-%H%M%S' 2>/dev/null)" != "$stamp" ]; then
            echo "skipping ${safe_name}: its name holds no real date"
            continue
        fi
        if [ "$epoch" -gt $((now + 600)) ]; then
            echo "skipping ${safe_name}: its name is dated more than 600s ahead of now"
            continue
        fi
        if [ ! -f "${dump}.hmac" ] || [ -L "${dump}.hmac" ] || [ "$(head -c 3 "${dump}.hmac" 2>/dev/null)" != "v2 " ]; then
            not_v2=$((not_v2 + 1))
            continue
        fi
        if [ -z "$newest" ] || [ "$epoch" -gt "$newest_epoch" ]; then
            newest=$dump
            newest_epoch=$epoch
        fi
    done
}

pick_newest_dump
if [ -z "$newest" ]; then
    [ "$not_v2" -eq 0 ] \
        || fail "${not_v2} dump(s) in ${BK} have a legacy sidecar or none - auto-restore uses only v2 dumps; restore one by hand with restore.sh"
    fail "no dumps in ${BK} - cannot auto-restore, restore manually"
fi
[ "$not_v2" -eq 0 ] || echo "passed over ${not_v2} dump(s) with a legacy sidecar or none"
age_h=$(( ( $(date +%s) - newest_epoch ) / 3600 ))
[ "$age_h" -le "$MAX_DUMP_AGE_HOURS" ] \
    || fail "newest dump is ${age_h}h old (cap ${MAX_DUMP_AGE_HOURS}h) - too stale to restore unattended; restore manually"
# A product that names a work folder wants the dump's plaintext there and nowhere
# else, so a folder that is not private refuses the restore, before anything is
# created, and never falls back to the default temp folder. Only this script
# reads the setting, not restore.sh: a drill may run restore.sh in a container,
# where a host path does not exist.
if [ -n "$RESTORE_WORK_DIR" ]; then
    problem=$(restore_work_dir_problem)
    [ -z "$problem" ] || fail "${problem} - refusing to restore"
fi
alert "database volume missing - auto-restoring $(basename "$newest") (${age_h}h old)"

cd "${REPO}/${COMPOSE_DIR}"

# From here on, failure must put the world back: `compose up postgres` creates
# ${PGDATA_VOLUME} EMPTY, and if the restore then dies (bad passphrase, corrupt
# dump, set -e on any step) that empty volume would make the next login's
# step 5 read "data is up" and hand the deployer an empty database - silently,
# forever. The trap removes the empty volume so every retry re-enters the
# restore path, and alerts, because an unattended recovery that fails quietly
# is worse than one that never ran.
# An EXIT trap with a success flag, NOT an ERR trap: ERR does not fire for
# failures on the left of ||, which is exactly how the fail() branches below
# are written - an ERR trap would strand the empty volume on a pg_isready
# timeout while cleaning up on every other failure shape.
RESTORE_OK=0
restore_cleanup() {
    [ "$RESTORE_OK" = "1" ] && return 0
    # First, so plaintext goes even if a docker call below runs to its limit.
    # This is an EXIT trap: the || true keeps the sweep out of the exit status.
    sweep_restore_work_dir || true
    timeout -k 10 120 docker compose -p "$COMPOSE_PROJECT" rm -sf "$DB_SERVICE" 9>&- >/dev/null 2>&1 || true
    timeout -k 10 10 docker volume rm "$PGDATA_VOLUME" 9>&- >/dev/null 2>&1 || true
    # Report what actually happened: a stop that runs past its limit leaves the
    # volume behind, and the next login would then hand the deployer an empty
    # database. A volume list that does not answer counts as not removed.
    volumes=$(timeout -k 10 10 docker volume ls --format '{{.Name}}' 9>&- 2>/dev/null) || volumes=$PGDATA_VOLUME
    # "its volume", not "the empty volume": a failed check after a complete
    # restore lands here too, and the cleanup cannot tell the two apart.
    if printf '%s\n' "$volumes" | grep -qx "$PGDATA_VOLUME"; then
        touch "$FAILED_MARK" || true
        alert "auto-restore FAILED and its volume ${PGDATA_VOLUME} was NOT removed - do not start the deployer; remove the volume and ${FAILED_MARK} by hand and investigate the log"
    else
        alert "auto-restore FAILED - removed its volume so the next login retries; investigate the log"
    fi
}
trap restore_cleanup EXIT

# -p "$COMPOSE_PROJECT" pinned (here and below), matching the deploy loop: the
# project name must never depend on what this directory happens to be called.
timeout -k 10 300 docker compose -p "$COMPOSE_PROJECT" up -d "$DB_SERVICE" 9>&-
for _ in $(seq 1 30); do
    timeout -k 10 10 docker compose -p "$COMPOSE_PROJECT" exec -T "$DB_SERVICE" pg_isready -U "$DB_USER" 9>&- >/dev/null 2>&1 && break
    sleep 3
done
timeout -k 10 10 docker compose -p "$COMPOSE_PROJECT" exec -T "$DB_SERVICE" pg_isready -U "$DB_USER" 9>&- >/dev/null 2>&1 \
    || fail "postgres never became ready during auto-restore"

# RESTORE_UNVERIFIED empty: an unattended restore must never pick up the
# manual-only escape hatch. RESTORE_REQUIRE_V2=1: pick_newest_dump chose a v2
# dump by its marker, but the files can change before this call, and a legacy
# pair put under the chosen name would verify and defeat the age cap; the
# authentication itself refuses one.
# RESTORE_PROJECT named rather than left to the
# default even though they agree: this call site really does mean the live
# stack, and restore.sh now announces whichever project it resolved, so the
# recovery log says which one this was.
#
# A bash prefix assignment, NOT `env VAR=value ...`: env is a real binary, so
# the value would be a literal argv element and visible in ps for the moment
# before the exec. backup.sh and restore.sh both say the passphrase travels by
# env and never argv; this is the line that used to break that rule, and it
# guards every encrypted dump including the off-site copies.
#
# TMPDIR is for restore.sh alone, the same way: the rest of this script, and its
# colima and docker calls, keep the environment they had. Without the setting
# the value is the folder restore.sh would pick by itself.
RESTORE_UNVERIFIED= RESTORE_REQUIRE_V2=1 RESTORE_PROJECT="$COMPOSE_PROJECT" PRODUCT_SETTINGS="$(dirname "$0")/product.settings" \
    TMPDIR="${RESTORE_WORK_DIR:-${TMPDIR:-/tmp}}" \
    BACKUP_PASSPHRASE="$(grep "^BACKUP_PASSPHRASE=" .env | cut -d= -f2-)" \
    timeout -k 10 1800 bash "${REPO}/${LIBRARY_DIR}/deploy/restore.sh" "$newest" 9>&-

# users>=1 is sufficient ONLY because restore.sh runs --single-transaction
# with ON_ERROR_STOP: the restore is all-or-nothing, so "users exists" implies
# "everything applied". If restore.sh ever loses that flag, this check loses
# its meaning with it.
# A count that does not answer is not an empty table. Any failure here still
# ends in the cleanup below, which removes the restored volume, so the message
# must say what really happened; and the limit is 60s rather than 10 because
# the good case answers in milliseconds while a false negative costs the next
# login a whole restore.
rc=0
USERS=$(timeout -k 10 60 docker compose -p "$COMPOSE_PROJECT" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" -t -A \
    -c "$RESTORE_VERIFY_SQL" 9>&-) || rc=$?
[ "$rc" -ne 124 ] && [ "$rc" -ne 137 ] \
    || fail "users count after the restore timed out after 60s or was killed (exit $rc) - stop and investigate"
[ "$rc" -eq 0 ] || fail "users count after the restore failed (exit $rc) - stop and investigate"
case "${USERS:-0}" in
    *[!0-9]*) fail "RESTORE_VERIFY_SQL did not return an integer: ${USERS}" ;;
esac
[ "${USERS:-0}" -ge 1 ] || fail "restore ran but users table is empty - stop and investigate"
echo "restored: users=${USERS}"
# The flag means "the data is good, keep it" - set BEFORE the deployer build,
# whose unrelated failure (registry blip, base-image pull) must not make the
# trap discard a correctly restored database. A deployer that fails to start
# is handled on the next login: step 5 finds the volume present and alerts.
RESTORE_OK=1
# The data is already kept; a marker that will not go must not end the run
# in silence, because the next login would refuse over a volume that is good.
rm -f "$FAILED_MARK" || alert "the restore succeeded but ${FAILED_MARK} could not be removed - remove it by hand or the next login refuses"

# The erasure boundary, on the channel a person actually sees. restore.sh
# prints it, but this path is unattended and its stdout is a log file that
# gets read after something else goes wrong - which is exactly when a
# deployment quietly holding data someone asked us to delete stays quiet.
#
# EXIT STATUS, not "is the answer empty". Postgres still starting, the
# container absent and a table with no rows all return the empty string, and
# they want opposite messages: the first two mean we could not check, the
# third means every erasure this deployment ever performed is undone. Sending
# the third on this path - a login, where postgres not being up yet is the
# normal condition - is the cry-wolf that gets the next real alert ignored.
#
# No pipeline, for the same reason: this file sets `set -eu` without pipefail,
# so `docker ... | tr` would report tr's status and every read would look like
# a success. Bash strips the \r instead.
#
# docker's stderr is left alone rather than sent to /dev/null: $( ) captures
# stdout only, so the reason a read failed reaches the launchd .err log, which
# is the only place it can be read - the notification truncates.
#
# A function so the three cases can be driven under a fake docker, the way
# test_autostart_volume_guard drives pgdata_present. The inline version could
# not be tested, and it was wrong.
erasure_clause() {
    local newest
    if newest=$(timeout -k 10 10 docker compose -p "$COMPOSE_PROJECT" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" \
        -tAc "$ERASURE_CHECK_SQL" 9>&-); then
        newest=${newest//$'\r'/}
        if [ -n "$newest" ]; then
            echo "any data-deletion request after ${newest}"
        else
            echo "EVERY data-deletion request this deployment ever received"
        fi
    else
        echo "an unknown set of data-deletion requests - the erasure record (ERASURE_CHECK_SQL) could not be read"
    fi
}

# The erasure clause leads: `display notification` truncates, and on this path
# the notification is the only channel a person sees. It does not say
# "identify those requests": the record went with the restore and nothing can
# enumerate them, which restore.sh's stderr explains at length.
[ -z "$ERASURE_CHECK_SQL" ] || alert "ERASURES UNDONE by auto-restore: $(erasure_clause) is gone from the database and its data is back - you cannot list them, so treat every account connected since as un-erased. (restore complete, users=${USERS}; the deployer build starts next)"

# After the erasure alert, not before: a build that fails or runs past its
# limit exits here, and the alert above must already have gone out.
timeout -k 10 900 docker compose -p "$COMPOSE_PROJECT" --profile "$OPS_PROFILE" up -d --build "$DEPLOYER_SERVICE" 9>&- \
    || fail "deployer build failed or ran past 900s (exit $?) - the restored data is kept; start the deployer manually (see README)"
