#!/bin/bash
# Operator-only disposable PostgreSQL 16/17 and generic archive verification.
# Uses the selected Docker daemon; creates only uniquely named drill resources.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
REPORT="${1:?usage: backup-restore-drill.sh REPORT_FILE HOST_BIND_ROOT}"
case "$REPORT" in /*) ;; *) REPORT="$PWD/$REPORT" ;; esac
umask 077
LOG_WORK=$(mktemp -d "${TMPDIR:-/tmp}/pipeline-drill-log.XXXXXX")
LOG_FILE=$(mktemp "$(dirname "$REPORT")/.pipeline-drill-report.XXXXXX")
WORK=""
PROJECT=""
IMAGE=""
cleanup() {
    local status=$?
    trap - EXIT
    if [ -n "$WORK" ] && [ -f "$WORK/compose.yml" ]; then
        docker compose --env-file /dev/null -f "$WORK/compose.yml" \
            -p "$PROJECT" down --volumes >/dev/null 2>&1 || status=1
    fi
    if [ -n "$IMAGE" ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
        if [ "$(docker image inspect --format '{{index .Config.Labels "org.example.pipeline-drill"}}' "$IMAGE")" = "$PROJECT" ]; then
            docker image rm "$IMAGE" >/dev/null 2>&1 || status=1
        else
            echo 'REFUSED: drill image ownership label does not match' >&2
            status=1
        fi
    fi
    [ -z "$WORK" ] || rm -rf "$WORK" || status=1
    rm -rf "$LOG_WORK" || status=1
    if [ "$status" -eq 0 ]; then
        printf 'PASS: backup restore drill\n'
    else
        printf 'FAIL: backup restore drill\n'
    fi
    if ! mv -f "$LOG_FILE" "$REPORT"; then
        printf 'FAIL: backup restore drill\n' >&3
        rm -f "$LOG_FILE" || :
        exit 1
    fi
    cat "$REPORT" >&3 || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
exec 3>&1
exec >"$LOG_FILE" 2>&1
commit_sha=$(git -C "$REPO" rev-parse HEAD)
case "$commit_sha" in *[!0-9a-f]*) echo 'REFUSED: invalid source commit'; exit 1 ;; esac
[ "${#commit_sha}" -eq 40 ] || { echo 'REFUSED: invalid source commit'; exit 1; }
printf 'Commit: %s\n' "$commit_sha"
HOST_BIND_ROOT="${2:?usage: backup-restore-drill.sh REPORT_FILE HOST_BIND_ROOT}"
case "$HOST_BIND_ROOT" in /*) ;; *) echo 'REFUSED: host bind root must be absolute' >&2; exit 1 ;; esac
[ -d "$HOST_BIND_ROOT" ] && [ ! -L "$HOST_BIND_ROOT" ] || { echo 'REFUSED: host bind root must be an existing plain directory' >&2; exit 1; }
HOST_BIND_ROOT="$(cd "$HOST_BIND_ROOT" && pwd -P)"
[ -n "$(find "$HOST_BIND_ROOT" -maxdepth 0 -type d -user "$(id -u)" ! -perm -020 ! -perm -002)" ] \
    || { echo 'REFUSED: host bind root must be owned by this user and not writable by others' >&2; exit 1; }
cd "$REPO"
# Never inherit an operator's production Compose files, profiles or env files.
compgen -v COMPOSE_ >"$LOG_WORK/compose-selectors" || :
while IFS= read -r setting; do unset "$setting"; done <"$LOG_WORK/compose-selectors"
export COMPOSE_DISABLE_ENV_FILE=1
[ -n "${DOCKER_HOST:-}" ] || { echo 'REFUSED: set DOCKER_HOST explicitly to a disposable daemon' >&2; exit 1; }
unset DOCKER_CONTEXT
command -v docker >/dev/null
command -v openssl >/dev/null
command -v python3 >/dev/null
docker compose version >/dev/null
daemon_name=$(docker info --format '{{.Name}}' 2>/dev/null) || {
    echo 'REFUSED: the selected Docker daemon is not running or reachable' >&2; exit 1;
}
[ -n "$daemon_name" ] || { echo 'REFUSED: the selected Docker daemon returned no name' >&2; exit 1; }
printf 'Docker daemon: %s\n' "$daemon_name"
printf 'Docker daemon: %s\n' "$daemon_name" >&3
export PIPELINE_DRILL_PASSPHRASE="disposable-fixture-$(date +%s)-$$"
export PIPELINE_DRILL_PASSWORD=$(openssl rand -hex 20)
export PIPELINE_DRILL_SALT=fakesalt
run_major() {
    local major=$1 postgres=$2 cipher source_counts restored_counts
    WORK=$(mktemp -d "$HOST_BIND_ROOT/pipeline-backup-drill.XXXXXX")
    local nonce
    nonce=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')
    PROJECT="pipeline-drill-${major}-$$-${nonce}"
    local resources name kind
    for kind in container network volume; do
        if [ "$kind" = container ]; then
            resources=$(docker "$kind" ls -aq --filter "label=com.docker.compose.project=$PROJECT") || exit 1
        else
            resources=$(docker "$kind" ls -q --filter "label=com.docker.compose.project=$PROJECT") || exit 1
        fi
        [ -z "$resources" ] || { echo 'FAIL: unique drill project already has resources'; exit 1; }
    done
    for name in "${PROJECT}_default" "${PROJECT}_backups" "${PROJECT}_heartbeat" "${PROJECT}_lock" "${PROJECT}_archives" "${PROJECT}-source-1" "${PROJECT}-restored-1"; do
        case "$name" in
            *_default) kind=network ;;
            *-source-1|*-restored-1) kind=container ;;
            *) kind=volume ;;
        esac
        if docker "$kind" inspect "$name" >/dev/null 2>&1; then
            echo 'FAIL: a named drill resource already exists'; exit 1
        fi
    done
    local candidate="pipeline-backup-drill:${major}-$$-${nonce}"
    if docker image inspect "$candidate" >/dev/null 2>&1; then
        echo 'FAIL: unique drill image already exists'; exit 1
    fi
    IMAGE="$candidate"
    case "$PROJECT" in pipeline-drill-*) ;; *) exit 1 ;; esac
    mkdir -p "$WORK/build/scripts" "$WORK/build/logs/fixture" "$WORK/copies" "$WORK/archive"
    export PIPELINE_DRILL_HOST_BACKUPS="$WORK/host-backups"
    mkdir "$PIPELINE_DRILL_HOST_BACKUPS"
    printf '%s\n' "$nonce" > "$PIPELINE_DRILL_HOST_BACKUPS/.host-marker"
    cp "$REPO/deploy/backup/backup.sh" "$REPO/deploy/backup/logarchive.sh" \
        "$REPO/deploy/backup/crypto.sh" "$WORK/build/scripts/"
    printf '{"Name":"/fakestack-api-1"}\n' > "$WORK/build/logs/fixture/config.v2.json"
    # The nonce keeps this COPY layer out of the build cache: a cached layer keeps
    # the file times of the run that built it, and logarchive.sh skips a log not
    # written to in the last day.
    printf 'disposable log archive fixture %s\n' "$nonce" > "$WORK/build/logs/fixture/fixture-json.log"
    cp "$REPO/deploy/backup/Dockerfile" "$WORK/build/Dockerfile"
    printf '\nCOPY scripts/ /scripts/\nCOPY logs/ /fixture-logs/\n' >> "$WORK/build/Dockerfile"
    docker build --build-arg "POSTGRES_IMAGE=$postgres" \
        --label "org.example.pipeline-drill=$PROJECT" -t "$IMAGE" "$WORK/build"
    export PIPELINE_DRILL_IMAGE="$IMAGE" PIPELINE_DRILL_POSTGRES="$postgres"
    cat > "$WORK/compose.yml" <<'YAML'
services:
  source:
    image: ${PIPELINE_DRILL_POSTGRES:?}
    user: postgres
    environment: &database
      POSTGRES_USER: fakeowner
      POSTGRES_DB: fake_test
      POSTGRES_PASSWORD: ${PIPELINE_DRILL_PASSWORD:?}
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs:
      - /var/lib/postgresql/data:uid=70,gid=70,mode=0700
      - /var/run/postgresql:uid=70,gid=70,mode=0770
      - /tmp
    healthcheck:
      test: [CMD-SHELL, pg_isready -U fakeowner -d fake_test]
      interval: 1s
      timeout: 3s
      retries: 60
  restored:
    image: ${PIPELINE_DRILL_POSTGRES:?}
    user: postgres
    environment: *database
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs:
      - /var/lib/postgresql/data:uid=70,gid=70,mode=0700
      - /var/run/postgresql:uid=70,gid=70,mode=0770
      - /tmp
    healthcheck:
      test: [CMD-SHELL, pg_isready -U fakeowner -d fake_test]
      interval: 1s
      timeout: 3s
      retries: 60
  backup:
    image: ${PIPELINE_DRILL_IMAGE:?}
    entrypoint: [/bin/sh, /scripts/backup.sh]
    environment:
      DB_SERVICE: source
      DB_NAME: fake_test
      DB_BACKUP_USER: fakereader
      PGPASSWORD: ${PIPELINE_DRILL_PASSWORD:?}
      BACKUP_PASSPHRASE: ${PIPELINE_DRILL_PASSPHRASE:?}
      BACKUP_HMAC_SALT: ${PIPELINE_DRILL_SALT:?}
      BACKUP_PREFIX: fakedump
      BACKUP_KEEP_DAYS: '14'
      BACKUP_MAX_DAYS: '14'
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    volumes:
      - backups:/backups
      - type: bind
        source: ${PIPELINE_DRILL_HOST_BACKUPS:?}
        target: /host-backups
        bind:
          create_host_path: false
      - heartbeat:/heartbeat
      - lock:/lock
    tmpfs: [/tmp]
  logarchive:
    image: ${PIPELINE_DRILL_IMAGE:?}
    entrypoint: [/bin/sh, /scripts/logarchive.sh]
    environment:
      COMPOSE_PROJECT: fakestack
      BACKUP_PASSPHRASE: ${PIPELINE_DRILL_PASSPHRASE:?}
      BACKUP_HMAC_SALT: ${PIPELINE_DRILL_SALT:?}
      DOCKER_LOGS: /fixture-logs
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    volumes: [archives:/log-archive]
    tmpfs: [/tmp, /stage]
volumes:
  backups:
  heartbeat:
  lock:
  archives:
YAML
    local -a C=(docker compose --env-file /dev/null -f "$WORK/compose.yml" -p "$PROJECT")
    if ! "${C[@]}" run --rm -T --no-deps --entrypoint /bin/sh \
        -e "PIPELINE_DRILL_BIND_PROBE=$nonce" backup -c '
        test "$(cat /host-backups/.host-marker)" = "$PIPELINE_DRILL_BIND_PROBE" &&
        printf "%s\n" "$PIPELINE_DRILL_BIND_PROBE" > /host-backups/.container-marker &&
        ln /host-backups/.container-marker /host-backups/.container-marker-link'; then
        echo 'REFUSED: host bind sharing or hard links could not be confirmed'; exit 1
    fi
    if [ "$(cat "$PIPELINE_DRILL_HOST_BACKUPS/.container-marker" 2>/dev/null)" != "$nonce" ] \
        || [ ! "$PIPELINE_DRILL_HOST_BACKUPS/.container-marker" -ef "$PIPELINE_DRILL_HOST_BACKUPS/.container-marker-link" ]; then
        echo 'REFUSED: host bind sharing or hard links could not be confirmed'; exit 1
    fi
    printf 'Host bind sharing and hard links verified: %s\n' "$PIPELINE_DRILL_HOST_BACKUPS"
    "${C[@]}" up -d --wait source restored
    for service in source restored; do
        printf "CREATE ROLE fakereader LOGIN PASSWORD '%s';\n" "$PIPELINE_DRILL_PASSWORD" \
            | "${C[@]}" exec -T "$service" psql -U fakeowner -d fake_test -v ON_ERROR_STOP=1
    done
    "${C[@]}" exec -T source psql -U fakeowner -d fake_test -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE alpha(id integer PRIMARY KEY, value text);
INSERT INTO alpha SELECT i, md5(i::text) FROM generate_series(1,500) i;
CREATE TABLE beta(id integer PRIMARY KEY, value text);
INSERT INTO beta SELECT i, repeat(md5(i::text),4) FROM generate_series(1,3000) i;
CREATE SEQUENCE checkpoint START 42;
GRANT CONNECT ON DATABASE fake_test TO fakereader;
GRANT USAGE ON SCHEMA public TO fakereader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fakereader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO fakereader;
SQL
    "${C[@]}" run --rm -T backup once
    "${C[@]}" run --rm -T --entrypoint /bin/sh backup \
        -c 'tar -C /backups -cf - .' | tar -xf - -C "$WORK/copies"
    cipher=$(find "$WORK/copies" -name 'fakedump-*.sql.gz.enc' -type f)
    [ -n "$cipher" ] && [ -f "$cipher.hmac" ]
    BACKUP_PASSPHRASE="$PIPELINE_DRILL_PASSPHRASE" BACKUP_HMAC_SALT=fakesalt \
        bash -c '. "$1"; backup_verify "$2"' fixture "$REPO/deploy/backup/crypto.sh" "$cipher"
    "${C[@]}" run --rm -T -e BACKUP_ROOT=/host-backups backup once
    cipher=$(find "$PIPELINE_DRILL_HOST_BACKUPS" -name 'fakedump-*.sql.gz.enc' -type f)
    [ -n "$cipher" ] && [ -f "$cipher.hmac" ]
    cp "$cipher" "$WORK/damaged.enc"
    cp "$cipher.hmac" "$WORK/damaged.enc.hmac"
    python3 - "$WORK/damaged.enc" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
b = bytearray(p.read_bytes())
b[32] ^= 1
p.write_bytes(b)
PY
    cat > "$WORK/product.settings" <<SETTINGS
PRODUCT=fakeproduct
COMPOSE_PROJECT=$PROJECT
DB_SERVICE=restored
DB_USER=fakeowner
DB_NAME=fake_test
BACKUP_HMAC_SALT=fakesalt
ERASURE_CHECK_SQL=
SETTINGS
    if (cd "$WORK" && COMPOSE_FILE="$WORK/compose.yml" COMPOSE_ENV_FILES=/dev/null \
        RESTORE_PROJECT="$PROJECT" RESTORE_SERVICE=restored RESTORE_UNVERIFIED=0 \
        PRODUCT_SETTINGS="$WORK/product.settings" BACKUP_PASSPHRASE="$PIPELINE_DRILL_PASSPHRASE" \
        bash "$REPO/deploy/restore.sh" "$WORK/damaged.enc"); then
        echo 'FAIL: a modified dump was accepted'; exit 1
    fi
    [ "$("${C[@]}" exec -T restored psql -U fakeowner -d fake_test -Atc \
        "SELECT count(*) FROM pg_tables WHERE schemaname='public'")" = 0 ]
    (cd "$WORK" && COMPOSE_FILE="$WORK/compose.yml" COMPOSE_ENV_FILES=/dev/null \
        RESTORE_PROJECT="$PROJECT" RESTORE_SERVICE=restored RESTORE_UNVERIFIED=0 \
        PRODUCT_SETTINGS="$WORK/product.settings" BACKUP_PASSPHRASE="$PIPELINE_DRILL_PASSPHRASE" \
        bash "$REPO/deploy/restore.sh" "$cipher")
    local query="SELECT count(*), md5(string_agg(id::text||value,'|' ORDER BY id)) FROM alpha; SELECT count(*), md5(string_agg(id::text||value,'|' ORDER BY id)) FROM beta; SELECT last_value FROM checkpoint;"
    source_counts=$("${C[@]}" exec -T source psql -U fakeowner -d fake_test -Atc "$query")
    restored_counts=$("${C[@]}" exec -T restored psql -U fakeowner -d fake_test -Atc "$query")
    [ "$source_counts" = "$restored_counts" ]
    printf 'PostgreSQL %s restored counts/checksums: %s\n' "$major" "$restored_counts"
    "${C[@]}" run --rm -T logarchive once
    "${C[@]}" run --rm -T --entrypoint /bin/sh logarchive \
        -c 'tar -C /log-archive -cf - .' | tar -xf - -C "$WORK/archive"
    local archive
    archive=$(find "$WORK/archive" -name 'logs-*.tar.gz.enc' -type f)
    [ -n "$archive" ]
    BACKUP_PASSPHRASE="$PIPELINE_DRILL_PASSPHRASE" BACKUP_HMAC_SALT=fakesalt \
        bash -c '. "$1"; backup_decrypt "$2"' fixture "$REPO/deploy/backup/crypto.sh" \
        "$archive" | tar -xOzf - './fakestack-api-1/fixture-json.log' \
        | grep -qx "disposable log archive fixture $nonce"
    "${C[@]}" down --volumes
    docker image rm "$IMAGE"
    rm -rf "$WORK"
    WORK="" PROJECT="" IMAGE=""
    printf 'Verified: PostgreSQL %s backup/restore, tamper refusal and log archive\n' "$major"
}
run_major 16 'postgres:16.15-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
run_major 17 'postgres:17.11-alpine@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b'
if grep -Fq "$PIPELINE_DRILL_PASSPHRASE" "$LOG_FILE" || \
    grep -Fq "$PIPELINE_DRILL_PASSWORD" "$LOG_FILE"; then
    echo 'FAIL: disposable secret appeared in drill output'; exit 1
fi
printf 'Verified: both supported PostgreSQL majors and generic log archives; resources removed\n'
