#!/bin/bash
# Poll-based continuous deployment: GitHub main + green CI -> pull -> compose up.
#
# Runs as a compose service (profile "ops") with the VM's docker socket. It
# deploys from ITS OWN CLONE under $SRC_DIR — never from anyone's
# working tree — so what serves production is exactly what GitHub has and CI
# blessed. Secrets are not in git: the operator's env files are mounted
# read-only and copied into the clone at deploy time.
#
# The mount path $SRC_DIR is IDENTICAL inside this container and in
# the VM. That is load-bearing: compose resolves the stack's relative bind
# mounts against the compose file's path as this client sees it, and the
# daemon then opens that same path on the VM side.
#
# Known freeze modes (all fail-closed, all show as "waiting on CI" in logs):
# a commit whose message contains [skip ci] creates zero workflow runs and is
# never deployed; a cancelled or skipped run also blocks. Push a normal commit
# to unfreeze. (Workflow runs, from the Actions API - not check runs. The gate
# reads /actions/runs; calling them checks is what once sent a PAT scope
# comment wrong.)
#
# Every product-specific value comes from the environment, which the product's
# compose file supplies with `env_file: product.settings` (see product.settings.example
# beside this directory). A missing key stops the loop here, before it acts.
set -euo pipefail

: "${GITHUB_DEPLOY_TOKEN:?GITHUB_DEPLOY_TOKEN must be set}"
: "${PRODUCT:?PRODUCT must be set (product.settings)}"
: "${GITHUB_REPO:?GITHUB_REPO must be set (product.settings)}"
: "${SRC_DIR:?SRC_DIR must be set (product.settings)}"
: "${COMPOSE_PROJECT:?COMPOSE_PROJECT must be set (product.settings)}"
: "${COMPOSE_DIR:?COMPOSE_DIR must be set (product.settings)}"
: "${LIBRARY_DIR:?LIBRARY_DIR must be set (product.settings)}"
: "${ENV_FILES:?ENV_FILES must be set (product.settings)}"
: "${APP_IGNORE_PATHS?APP_IGNORE_PATHS must be set, empty for none (product.settings)}"
# Optional include list for app_changed; empty means the whole tree.
APP_PATHS="${APP_PATHS:-}"
# The site's release marker is optional: empty means no site check at all.
# The site release hook runs only once the marker shows a new sha, so it
# needs the marker.
SITE_URL="${SITE_URL:-}"
if [ -z "$SITE_URL" ] && [ -n "${SITE_RELEASE_HOOK:-}" ]; then
    echo "SITE_RELEASE_HOOK needs SITE_URL: set SITE_URL, or leave SITE_RELEASE_HOOK empty (product.settings)" >&2
    exit 1
fi
: "${DB_SERVICE:?DB_SERVICE must be set (product.settings)}"
: "${DB_VOLUME:?DB_VOLUME must be set (product.settings)}"
: "${MIGRATE_SERVICE:?MIGRATE_SERVICE must be set (product.settings)}"
: "${MIGRATE_CHECK_CMD:?MIGRATE_CHECK_CMD must be set (product.settings)}"
: "${API_SERVICE:?API_SERVICE must be set (product.settings)}"
: "${API_READY_CMD:?API_READY_CMD must be set (product.settings)}"
# The worker readiness check is optional, but only as a pair: both set, or both empty.
WORKER_SERVICE="${WORKER_SERVICE:-}"
WORKER_READY_CMD="${WORKER_READY_CMD:-}"
ready_cmds="MIGRATE_CHECK_CMD API_READY_CMD"
if [ -n "$WORKER_SERVICE" ] && [ -n "$WORKER_READY_CMD" ]; then
    ready_cmds="$ready_cmds WORKER_READY_CMD"
elif [ -n "$WORKER_SERVICE" ] || [ -n "$WORKER_READY_CMD" ]; then
    echo "WORKER_SERVICE and WORKER_READY_CMD must be set together, or both left empty (product.settings)" >&2
    exit 1
fi
# A whitespace-only command would split into no words at all.
for cmd in $ready_cmds; do
    [ -n "${!cmd//[[:space:]]/}" ] || { echo "$cmd must not be blank (product.settings)" >&2; exit 1; }
done
REPO="$GITHUB_REPO"
BRANCH="${DEPLOY_BRANCH:-main}"
POLL="${DEPLOY_POLL_SECONDS:-90}"
SRC="${SRC_DIR}/repo"
STATE="${SRC_DIR}/deployed-sha"
# The compose project of the running stack, from its own directory in the clone.
COMPOSE_DIR_ABS="${SRC}/${COMPOSE_DIR}"
# Optional: a script in the clone, run after every successful `up`.
HOOK="${POST_DEPLOY_HOOK:-}"
# Optional: a script in the clone, run before anything is replaced.
PRE_HOOK="${PRE_DEPLOY_HOOK:-}"
# Optional: a script in the clone, run when the site serves a new sha.
SITE_HOOK="${SITE_RELEASE_HOOK:-}"
# The space-separated settings are split where they are used with read -a,
# which splits without globbing, so no word is matched against the working
# directory. Each ENV_FILES entry is copied from /host-env into the compose
# directory, so it must be a plain file name: a slash would read or write
# outside either.
read -r -a ENV_FILE_LIST <<<"$ENV_FILES"
for name in ${ENV_FILE_LIST[@]+"${ENV_FILE_LIST[@]}"}; do
    case "$name" in
        */*) echo "ENV_FILES entries must be file names without a slash: ${name}" >&2; exit 1 ;;
    esac
done
# The release policy (see render_policy): the workflow every release needs,
# and the post-release workflow the gate ignores.
TESTS_WORKFLOW="${TESTS_WORKFLOW:-.github/workflows/tests.yml}"
PROMOTE_WORKFLOW="${PROMOTE_WORKFLOW:-.github/workflows/promote.yml}"
ENVDIR="${DEPLOY_ENV_DIR:-/host-env}"
API="https://api.github.com/repos/${REPO}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Token passed to git/curl via env and stdin, never argv (stays out of
# /proc/*/cmdline) and never persisted into the clone's git config. The
# extraheader key is host-scoped so a cross-host redirect can't carry it.
# github.com's git smart-HTTP endpoints reject "Bearer" for fine-grained
# PATs (the REST API accepts it); git needs Basic with x-access-token.
GIT_B64="$(printf 'x-access-token:%s' "${GITHUB_DEPLOY_TOKEN}" | base64 | tr -d '\n')"
git_authed() {
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
    GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${GIT_B64}" \
    git "$@"
}

gh_curl() {
    curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" -K - "$@" \
        <<<"header = \"Authorization: Bearer ${GITHUB_DEPLOY_TOKEN}\""
}

log() { echo "[deployer] $(date -u +%FT%TZ) $*"; }

ALERT_URL="${DEPLOY_ALERT_URL:-}"
ALERTED="${SRC_DIR}/alerted-sha"
FAILED_STEP=""
# The rest of the pipeline, watched from here because this is the one place
# that holds the alert URL: the ci branch, where tests, the secret scan and
# promotion run (a sha that fails there never reaches main, so the main loop
# below would never see it), and the site build, which publishes
# every main sha to the release marker at SITE_URL (when one is set).
CI_BRANCH="${SOURCE_BRANCH:-ci}"
CI_ALERTED="${SRC_DIR}/ci-alerted-sha"
SITE_GRACE="${DEPLOY_SITE_GRACE_SECONDS:-1500}"
SITE_PENDING="${SRC_DIR}/site-pending"
SITE_ALERTED="${SRC_DIR}/site-alerted-sha"
# The last sha the site release hook succeeded for. Every run is handed it,
# so a hook can cover the releases from there to the sha the site has just
# published, and a failed or superseded release is carried by the next one
# rather than lost.
SITE_HOOKED="${SRC_DIR}/site-hook-sha"
# The files the Dockerfile copies into this image (check_self compares them
# with the clone's), the differing ones last reported, and the ones last
# logged as impossible to compare. The report is remembered in the shell, so
# it is made once even when the file cannot be written or removed; the file
# is read once, on the first check, and only carries it across a restart.
BAKED=(deploy-loop.sh ci-gate.sh ci-gate.jq)
BEHIND="${SRC_DIR}/deployer-behind"
BEHIND_READ=""
BEHIND_SAID=""
UNCOMPARED=""

alert_url_ok() {
    # The URL is spliced into a curl config line, where a newline would start
    # a second directive; a Telegram URL never needs anything outside this set.
    local allowed='A-Za-z0-9._~:/?=&%+-'
    case "$ALERT_URL" in
        *[!$allowed]*) return 1 ;;
    esac
}

alert() {
    # One optional webhook, Telegram-shaped: the URL carries the bot token and
    # chat id, so it travels by stdin config like the GitHub token, never argv.
    # Fail-open on purpose: a lost notification must never fail or delay a deploy.
    # Telegram sometimes answers slower than the timeout (two deploy messages
    # were lost that way on 2026-09-12); a timeout is transient to curl, so
    # --retry gives it two more tries before the message is given up on.
    [ -n "$ALERT_URL" ] || return 0
    alert_url_ok || { log "DEPLOY_ALERT_URL has unexpected characters; alerts off"; return 0; }
    curl -fsS --max-time 10 --retry 2 --retry-delay 2 --data-urlencode "text=$*" -K - <<<"url = \"${ALERT_URL}\"" >/dev/null 2>&1 \
        || log "alert delivery failed"
}

notify_failure() {
    # Once per failing sha, not once per tick: the loop retries every $POLL s.
    local sha="$1"
    [ "$(cat "$ALERTED" 2>/dev/null)" = "$sha" ] && return 0
    echo "$sha" 2>/dev/null >"$ALERTED" || log "could not write ${ALERTED}"
    alert "${PRODUCT} deploy FAILED at ${FAILED_STEP:-unknown step} for ${sha:0:7}; retrying every ${POLL}s; see docker logs ${COMPOSE_PROJECT}-deployer-1"
}

forget() {
    # rm -f answers 1 for a file that exists and cannot be unlinked, which
    # under set -e would end the loop; a state file that will not go is a
    # log line, never the deployer.
    rm -f "$1" 2>/dev/null || log "could not remove $1"
    return 0
}

subject_of() {
    # The commit's first line, for the message; empty rather than failing.
    git -C "$SRC" log -1 --format=%s "$1" -- 2>/dev/null | cut -c1-90 || true
}

notify_deployed() {
    # Alerts are for failures only: a successful deploy is a log line, except
    # one that ends a run of failures, which closes that incident with a message.
    local sha="$1" subject
    subject=$(subject_of "$sha")
    if [ -f "$ALERTED" ]; then
        forget "$ALERTED"
        alert "${PRODUCT} deploy recovered: ${sha:0:7} is live - ${subject}"
    else
        log "deployed ${sha:0:7} - ${subject}"
    fi
}

notify_no_app_change() {
    # Says what is true of both cases rather than naming one of them. This
    # used to report every skip as a site-only change, and a phone-app commit
    # described that way is a line somebody reads at three in the morning and
    # then mistrusts everything after.
    local sha="$1"
    log "${sha:0:7} changes nothing this server runs - $(subject_of "$sha"); site build pending"
}

ci_failures() {
    # Every completed run for one sha that did not succeed, one line each.
    # Post-release runs count too: a failed promotion or indexing run is a
    # pipeline failure even though the gate ignores them.
    gh_curl "${API}/actions/runs?head_sha=$1&per_page=100" \
        | jq -r '.workflow_runs[] | select(.event != "dynamic")
                 | select(.status == "completed" and .conclusion != "success")
                 | "\(.path) \(.conclusion) \(.html_url)"'
}

check_ci() {
    # Once per sha at the head of the ci branch. A sha with no failure is
    # asked again next tick, because its runs finish over the following
    # minutes; a read that fails is retried next tick.
    local sha failures
    sha=$(gh_curl "${API}/commits/${CI_BRANCH}" | jq -r .sha) || return 0
    [ -n "$sha" ] && [ "$sha" != "null" ] || return 0
    [ "$(cat "$CI_ALERTED" 2>/dev/null)" = "$sha" ] && return 0
    failures=$(ci_failures "$sha") || return 0
    [ -n "$failures" ] || return 0
    echo "$sha" 2>/dev/null >"$CI_ALERTED" || log "could not write ${CI_ALERTED}"
    alert "${PRODUCT} pipeline FAILED for ${sha:0:7} on ${CI_BRANCH}: $(printf '%s' "$failures" | tr '\n' ';')"
}

mark_site_pending() {
    # Superseded silently: a newer sha replaces an older one still pending,
    # and takes its own alert state with it. Fail-open: a marker that could
    # not be written must not turn a live deploy into a reported failure.
    # No release marker configured: nothing to wait for.
    [ -n "$SITE_URL" ] || return 0
    forget "$SITE_ALERTED"
    printf '%s %s\n' "$1" "$(date +%s)" 2>/dev/null >"$SITE_PENDING" || log "could not write ${SITE_PENDING}"
    return 0
}

check_site() {
    # The release marker carries the sha the site was last built from. Published
    # inside the grace period is a success message; past it, one alert per
    # sha, and a recovery message when it lands after all. No release marker
    # configured: never polled, never alerted.
    [ -n "$SITE_URL" ] || return 0
    [ -f "$SITE_PENDING" ] || return 0
    local sha="" stamped="" live now
    # read fails on an empty file or a line with no newline, and under set -e
    # a bare failure here would end the loop for good, before any alert could
    # say so. A marker with no sha is dropped; one with no usable stamp
    # restarts its grace period rather than alerting at once.
    read -r sha stamped _ <"$SITE_PENDING" || true
    [ -n "$sha" ] || { forget "$SITE_PENDING"; return 0; }
    [[ "$stamped" =~ ^[0-9]+$ ]] || { mark_site_pending "$sha"; return 0; }
    # The marker is public and would be attacker-controlled if the site were;
    # it is compared as a sha and echoed into a message, nothing more.
    live=$(curl -fsS --max-time 15 --max-filesize 4096 "$SITE_URL" 2>/dev/null | jq -r .sha 2>/dev/null) || live=""
    [[ "$live" =~ ^[0-9a-f]{40}$ ]] || live=""
    if [ "$live" = "$sha" ]; then
        forget "$SITE_PENDING"
        if [ "$(cat "$SITE_ALERTED" 2>/dev/null)" = "$sha" ]; then
            forget "$SITE_ALERTED"
            alert "${PRODUCT} site build recovered: ${sha:0:7} is published"
        else
            log "site published ${sha:0:7}"
        fi
        run_site_hook "$sha"
        return 0
    fi
    now=$(date +%s)
    if [ $((now - 10#$stamped)) -ge "$SITE_GRACE" ] && [ "$(cat "$SITE_ALERTED" 2>/dev/null)" != "$sha" ]; then
        echo "$sha" 2>/dev/null >"$SITE_ALERTED" || log "could not write ${SITE_ALERTED}"
        alert "${PRODUCT} site build has not published ${sha:0:7} after $((SITE_GRACE / 60)) min (live: ${live:-unreadable}); check the site build"
    fi
}

hook_path_ok() {
    # A hook is a path inside the clone: not absolute and with no .. segment.
    # (Whether it is a symlink is checked where it runs, at that sha.)
    case "/$1/" in
        //*|*/../*) return 1 ;;
    esac
    return 0
}

run_site_hook() {
    # The product's step for a published release (telling search engines
    # which pages changed, say), run now that the site serves this sha, with
    # the sha as $1 and the last sha the hook succeeded for as $2 (empty when
    # there is none; what to do then is the hook's call). On a failure the
    # marker stays, one alert goes out, and the next published sha hands the
    # hook the wider range. Never fatal, like every other pipeline watch here.
    local sha="$1" before
    [ -n "$SITE_HOOK" ] || return 0
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
    before="$(cat "$SITE_HOOKED" 2>/dev/null || true)"
    [[ "$before" =~ ^[0-9a-f]{40}$ ]] || before=""
    [ "$before" = "$sha" ] && return 0
    # The hook runs from the clone at this sha. A site-only sha was recorded
    # without a checkout, so make one. Safe for the files compose binds from
    # this tree: the published sha is the recorded one, and a recorded sha
    # differs from the deployed tree only in the paths app_changed excludes.
    # The checkout's errors are left on the log, which the failure alert
    # points at.
    git -C "$SRC" cat-file -e "${sha}^{commit}" 2>/dev/null \
        || git_authed -C "$SRC" fetch origin "$BRANCH" 2>/dev/null || true
    if ! git -C "$SRC" checkout -q "$sha"; then
        alert "${PRODUCT} site release hook FAILED for ${sha:0:7} (checkout); the next published sha retries from ${before:0:7}; see docker logs ${COMPOSE_PROJECT}-deployer-1"
        return 0
    fi
    # Set but not there at this sha is a failed run, never a silent skip; a
    # symlink could point anywhere, so it is refused the same way.
    if [ -L "$SRC/$SITE_HOOK" ] || [ ! -f "$SRC/$SITE_HOOK" ] || [ ! -x "$SRC/$SITE_HOOK" ]; then
        alert "${PRODUCT} site release hook FAILED for ${sha:0:7}: ${SITE_HOOK} is missing or not executable, or is a symlink; the next published sha retries from ${before:0:7}"
        return 0
    fi
    log "running site release hook ${SITE_HOOK}"
    # From the clone's root, so the hook's relative paths are the repository's,
    # and without the GitHub token or the alert URL, which a hook never needs.
    if (cd "$SRC" && env -u GITHUB_DEPLOY_TOKEN -u DEPLOY_ALERT_URL \
        timeout 900 "$SRC/$SITE_HOOK" "$sha" "$before"); then
        echo "$sha" 2>/dev/null >"$SITE_HOOKED" || log "could not write ${SITE_HOOKED}"
        log "site release hook done for ${before:0:7}..${sha:0:7}"
    else
        alert "${PRODUCT} site release hook FAILED for ${sha:0:7}; the next published sha retries from ${before:0:7}; see docker logs ${COMPOSE_PROJECT}-deployer-1"
    fi
    return 0
}

render_policy() {
    # The release policy ci-gate.sh reads from its own folder, built from the
    # settings in the same shape reusable-promote.yml writes, so promotion
    # and deployment apply one gate. Written into the image's own folder at
    # start, never read from the clone: a commit must not relax its own gate.
    jq -n --arg tests "$TESTS_WORKFLOW" --arg source "$CI_BRANCH" --arg target "$BRANCH" \
        --arg post "$PROMOTE_WORKFLOW" \
        '{required_workflows: [$tests], release_branches: [$source, $target],
          post_release_workflows: ($post | split(" ") | map(select(. != "")))}' \
        >"$SCRIPT_DIR/ci-policy.json"
}

check_self() {
    # This container never replaces itself (profile ops), so a release that
    # changes the files baked into its image keeps running the old ones until
    # someone does the rollout. Say so, once per set of differing files. The
    # clone's copies are only compared, never run, and nothing here is fatal.
    local name copy status differ="" unread="" text
    for name in "${BAKED[@]}"; do
        copy="$SRC/$LIBRARY_DIR/deploy/deployer/$name"
        # A plain file only: cmp follows a symlink, and one that points at a
        # pipe would hold this loop for ever.
        if [ ! -f "$copy" ] || [ -L "$copy" ]; then
            unread="${unread:+$unread }$name"
            continue
        fi
        status=0
        cmp -s "$SCRIPT_DIR/$name" "$copy" 2>/dev/null || status=$?
        case "$status" in
            0) ;;
            1) differ="${differ:+$differ }$name" ;;
            *) unread="${unread:+$unread }$name" ;;
        esac
    done
    if [ "$unread" != "$UNCOMPARED" ]; then
        UNCOMPARED="$unread"
        [ -z "$unread" ] || log "could not compare the deployer image with the repository: ${unread}"
    fi
    if [ -z "$BEHIND_READ" ]; then
        BEHIND_READ=1
        BEHIND_SAID="$(cat "$BEHIND" 2>/dev/null || true)"
    fi
    [ "$differ" = "$BEHIND_SAID" ] && return 0
    if [ -n "$differ" ]; then
        BEHIND_SAID="$differ"
        echo "$differ" 2>/dev/null >"$BEHIND" || log "could not write ${BEHIND}"
        text="deployer image does not match the repository (differing: ${differ}); do the deployer rollout (product runbook)"
        log "$text"
        alert "${PRODUCT} ${text}"
    elif [ -z "$unread" ]; then
        # Nothing differs and everything could be read: the rollout happened.
        BEHIND_SAID=""
        forget "$BEHIND"
        log "deployer image matches the repository again"
        alert "${PRODUCT} deployer image matches the repository again"
    fi
    return 0
}

ensure_clone() {
    if [ ! -d "$SRC/.git" ]; then
        log "cloning ${REPO}"
        git_authed clone --branch "$BRANCH" "https://github.com/${REPO}.git" "$SRC"
    fi
}

latest_sha() {
    gh_curl "${API}/commits/${BRANCH}" | jq -r .sha
}

ci_green() {
    # Read-only Actions evidence; promotion and deployment share the same
    # expected workflow paths, source restrictions and fail-closed verdict.
    gh_curl "${API}/actions/runs?head_sha=$1&per_page=100" \
        | bash "$SCRIPT_DIR/ci-gate.sh" "$1" "$REPO"
}

sync_env() {
    # The env files reach the clone by copy, so the stack reads them from one
    # place. When this container was recreated from inside itself, compose
    # resolved `./.env` against the clone and
    # the mount IS the destination: cp refuses "the same file", which must not
    # fail the deploy - the clone already holds exactly what the mount shows.
    # Names only (checked at start), split without globbing.
    local name names
    read -r -a names <<<"$ENV_FILES"
    for name in ${names[@]+"${names[@]}"}; do
        if [ "$ENVDIR/$name" -ef "$COMPOSE_DIR_ABS/$name" ]; then
            log "WARNING: $ENVDIR/$name is the clone's own copy; host edits will not reach the stack until the deployer is recreated from the host ${COMPOSE_DIR}/ directory"
            continue
        fi
        cp "$ENVDIR/$name" "$COMPOSE_DIR_ABS/$name" || return 1
    done
}

schema_compatible() {
    # Deploying a commit whose migrations do not include the version the
    # database is already at would run older code against a newer schema.
    # Refuse it: the way back is a newer commit or a restore, never a silent
    # downgrade. One state is compatible without a comparison, and it is
    # logged: no database container yet (first deploy on a fresh VM). A read
    # that fails for any other reason refuses, because a safety check that
    # skips itself quietly is not one.
    local containers check
    # -a: a stopped database still holds a schema to compare against.
    containers=$(docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" ps -aq "$DB_SERVICE" 2>&1) || {
        log "REFUSED: could not list the ${DB_SERVICE} container (${containers:-no output})"
        return 1
    }
    if [ -z "$containers" ]; then
        # A container can be removed while its named volume survives, and the
        # volume is where the data is. Treating "no container" as "fresh VM"
        # would skip the refusal below and attach a new container, carrying
        # whatever migrations the candidate commit holds, to the old data.
        #
        # Captured rather than piped straight into the `if`. Piped, a failing
        # `docker volume ls` produces no output, so grep matches nothing and
        # exits 1, the test is false, and the branch falls through to
        # "proceed" - a docker that cannot answer reads exactly like a machine
        # with no volume. (Not a pipefail subtlety: grep's own exit status
        # does it, with or without pipefail.) This was the one read in a
        # function whose rule is that a check which skips itself is not one.
        # -qx, so a volume whose name merely
        # contains this one cannot answer for it.
        local volumes
        volumes=$(docker volume ls --format '{{.Name}}' 2>&1) || {
            log "REFUSED: could not list volumes (${volumes:-no output})"
            return 1
        }
        if printf '%s\n' "$volumes" | grep -qx "${COMPOSE_PROJECT}_${DB_VOLUME}"; then
            log "REFUSED: no ${DB_SERVICE} container but ${COMPOSE_PROJECT}_${DB_VOLUME} exists - start ${DB_SERVICE}, or remove the volume if it is stale"
            return 1
        fi
        log "schema check: no ${DB_SERVICE} container and no ${DB_VOLUME} volume, nothing to compare"
        return 0
    fi
    # The next deploy step starts the database anyway; start it now so it can be asked.
    docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" up -d "$DB_SERVICE" >/dev/null 2>&1 || {
        log "REFUSED: ${DB_SERVICE} would not start for the schema check"
        return 1
    }
    # The candidate commit's own migrate service decides: MIGRATE_CHECK_CMD,
    # run in MIGRATE_SERVICE, reads the database's migration state without
    # writing and exits 0 only when this commit's migrations can bring the
    # database to their head. The image is built first, so the check is the
    # candidate's and not the running one's.
    check=$(docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" build "$MIGRATE_SERVICE" 2>&1) || {
        log "REFUSED: the migrate image would not build for the schema check ($(printf '%s' "$check" | tail -n 3 | tr '\n' ' '))"
        return 1
    }
    # MIGRATE_CHECK_CMD split into words without globbing.
    local migrate_check
    read -r -a migrate_check <<<"$MIGRATE_CHECK_CMD"
    if check=$(docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" run --rm -T "$MIGRATE_SERVICE" \
        ${migrate_check[@]+"${migrate_check[@]}"} 2>&1); then
        # Drop compose's own "Container ... Running" lines from the record.
        log "schema check: $(printf '%s' "$check" | grep -v '^ *Container ' | tr '\n' ' ')"
        return 0
    fi
    log "REFUSED: ${check:-no output}; deploy a newer commit or restore from a backup taken at this commit (product runbook, Rollback)"
    return 1
}

deploy() {
    # Explicit `|| return 1` on every step: `deploy X || log ...` at the call
    # site suppresses errexit for the WHOLE function body (POSIX), so without
    # these a failed step would fall through and record the sha as deployed.
    local sha="$1"
    log "deploying ${sha}"
    FAILED_STEP="git fetch"
    git_authed -C "$SRC" fetch origin "$BRANCH" || return 1
    FAILED_STEP="git reset"
    git -C "$SRC" reset --hard "$sha" || return 1
    FAILED_STEP="env copy"
    sync_env || return 1
    FAILED_STEP="schema check"
    schema_compatible || return 1
    # The product's own pre-deploy step (a database backup, say), which must
    # succeed before anything running is replaced. Same rules as the
    # post-deploy hook below: from the clone at this sha, from its root, the
    # sha as its argument, without the token or the alert URL, under a
    # timeout. Fatal: a failure leaves the running stack as it is, is retried
    # next tick and alerted once. Set but missing, not executable or a
    # symlink fails the deploy, never a silent skip.
    if [ -n "$PRE_HOOK" ]; then
        FAILED_STEP="pre-deploy hook"
        if [ -L "$SRC/$PRE_HOOK" ] || [ ! -f "$SRC/$PRE_HOOK" ] || [ ! -x "$SRC/$PRE_HOOK" ]; then
            log "pre-deploy hook ${PRE_HOOK} is missing or not executable, or is a symlink"
            return 1
        fi
        log "running pre-deploy hook ${PRE_HOOK}"
        (cd "$SRC" && env -u GITHUB_DEPLOY_TOKEN -u DEPLOY_ALERT_URL \
            timeout 900 "$SRC/$PRE_HOOK" "$sha") || return 1
    fi
    # -p COMPOSE_PROJECT: pin the project name so this always adopts the one running
    # stack (and its volumes) no matter where the compose file lives.
    # No profile selected, so the ops profile (this container) is excluded:
    # the deployer never rebuilds or recreates itself mid-run.
    # Build first, separately from `up`, so the archive below runs after it:
    # the running containers keep logging through a build, and an archive
    # taken before the build would lose those lines. A failed build stops
    # here with the running stack untouched.
    FAILED_STEP="compose build"
    docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" build || return 1
    # Archive the running containers' logs after the build and before compose
    # recreates any of them: docker deletes a removed container's log files
    # with it, and the nightly archive would miss everything since the
    # previous night. Best effort on purpose: the deploy must not depend on
    # the archiver (the release that introduces it has no archiver to call
    # yet). The timeout runs INSIDE the container, so a stuck archive dies
    # rather than holding this loop: an unreachable bucket alone can cost ten
    # minutes.
    docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" exec -T logarchive \
        timeout 900 sh /scripts/logarchive.sh once \
        || log "pre-deploy log archive failed: lines since the last nightly archive may be lost"
    # --remove-orphans: a service deleted from the compose file would
    # otherwise keep running. A
    # service of an unselected profile, this deployer included, is still
    # defined and never an orphan (compose v5.6.1 counts disabled services
    # as defined: isOrphaned, pkg/compose/containers.go).
    # --no-build: the images were built above, before the archive, so nothing
    # here delays the recreate and loses lines logged since the archive.
    FAILED_STEP="compose up"
    docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" up -d --no-build --remove-orphans || return 1
    # The product's own post-deploy steps (a proxy reload, its own readiness
    # checks), from the clone at this sha, with this loop's environment and
    # the sha as its argument. Fatal like every step here: a failure is
    # retried next tick and alerted once. The timeout keeps a stuck hook from
    # holding this loop.
    # Set but missing or not executable fails the deploy, never a silent skip.
    # Run from the clone's root, so the hook's relative paths are the repository's.
    if [ -n "$HOOK" ]; then
        FAILED_STEP="post-deploy hook"
        # A symlink could point anywhere: refused like a missing file.
        if [ -L "$SRC/$HOOK" ] || [ ! -f "$SRC/$HOOK" ] || [ ! -x "$SRC/$HOOK" ]; then
            log "post-deploy hook ${HOOK} is missing or not executable, or is a symlink"
            return 1
        fi
        log "running post-deploy hook ${HOOK}"
        # Without the GitHub token or the alert URL, which a hook never needs.
        (cd "$SRC" && env -u GITHUB_DEPLOY_TOKEN -u DEPLOY_ALERT_URL \
            timeout 900 "$SRC/$HOOK" "$sha") || return 1
    fi
    # Starting a container is not readiness. Do not advance the release marker
    # until the API can answer and its dependencies are reachable:
    # API_READY_CMD runs inside API_SERVICE and exits 0 only when it is ready.
    # Both readiness commands are split into words without globbing.
    local api_ready worker_ready
    read -r -a api_ready <<<"$API_READY_CMD"
    read -r -a worker_ready <<<"$WORKER_READY_CMD"
    FAILED_STEP="${API_SERVICE} readiness"
    docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" exec -T "$API_SERVICE" \
        ${api_ready[@]+"${api_ready[@]}"} || return 1
    # The worker: WORKER_READY_CMD checks its own registration and dependencies.
    if [ -n "$WORKER_SERVICE" ]; then
        FAILED_STEP="worker readiness"
        docker compose -p "$COMPOSE_PROJECT" --project-directory "$COMPOSE_DIR_ABS" exec -T "$WORKER_SERVICE" \
            ${worker_ready[@]+"${worker_ready[@]}"} || return 1
    else
        log "no worker readiness check configured"
    fi
    # A marker that could not be written is a deploy not recorded: say so
    # once and retry next tick, rather than announcing the same deploy every
    # tick against a stale marker.
    FAILED_STEP="record"
    echo "$sha" 2>/dev/null >"$STATE" || return 1
    docker image prune -f >/dev/null || true
    log "deployed ${sha}"
    notify_deployed "$sha"
    mark_site_pending "$sha"
}

app_changed() {
    # Not everything in a repo is this server. A marketing site, a phone app
    # or Markdown files may ride along in the same history, and a diff
    # entirely inside them does not warrant recreating the app stack - a
    # restart drops connections and re-runs migrations for a change to files
    # this container never reads. APP_IGNORE_PATHS lists those paths (git
    # pathspecs, space-separated); empty, every change deploys. APP_PATHS,
    # when set, narrows the diff to those paths first (an include list), and
    # APP_IGNORE_PATHS still excludes from within them.
    #
    # Anything ambiguous - a first deploy, a missing object, a diff that
    # errors - counts as changed, so the failure direction is a harmless
    # redeploy rather than a silent skip of a real one. An APP_PATHS pathspec
    # that matches nothing at the sha is the one exception: it answers 2, a
    # failed attempt (see below).
    local current="$1" sha="$2" path spec ignore includes=() excludes=()
    read -r -a includes <<<"${APP_PATHS:-}"
    # read -a splits without globbing, so a pathspec such as *.md stays literal.
    read -r -a ignore <<<"$APP_IGNORE_PATHS"
    # The +-form keeps an empty list from being "unbound" under set -u on old bash.
    for path in ${ignore[@]+"${ignore[@]}"}; do
        excludes+=(":!${path}")
    done
    [ -z "$current" ] && return 0
    git -C "$SRC" cat-file -e "${sha}^{commit}" 2>/dev/null \
        || git_authed -C "$SRC" fetch origin "$BRANCH" || return 0
    # Every APP_PATHS pathspec must match a file in this sha's tree (the diff
    # from the empty tree lists all of it). One that matches nothing - a typo,
    # a folder renamed - would leave every diff empty and record every sha as
    # "no app change", so the server would never redeploy and nothing would
    # say so. It fails the attempt instead: alerted, not recorded, retried
    # next tick. A git error fails the same way, and its message stays on
    # the log.
    for spec in ${includes[@]+"${includes[@]}"}; do
        if [ -z "$(git -C "$SRC" diff --name-only "$EMPTY_TREE" "$sha" -- "$spec")" ]; then
            log "APP_PATHS pathspec ${spec} matches no file at ${sha}"
            FAILED_STEP="APP_PATHS"
            return 2
        fi
    done
    git -C "$SRC" cat-file -e "${current}^{commit}" 2>/dev/null || return 0
    # An empty (or blank) APP_PATHS keeps the whole-tree diff.
    [ "${#includes[@]}" -eq 0 ] && includes=(.)
    if git -C "$SRC" diff --quiet "$current" "$sha" -- "${includes[@]}" ${excludes[@]+"${excludes[@]}"} 2>/dev/null; then
        return 1
    fi
    return 0
}

# Hook paths stay inside the clone. A bad deploy hook stops the loop here,
# as a missing one fails every deploy; a bad site hook is alerted and skipped,
# as a missing one is.
if [ -n "$HOOK" ] && ! hook_path_ok "$HOOK"; then
    log "POST_DEPLOY_HOOK must be a path inside the repository (not absolute, no ..): ${HOOK}"
    exit 1
fi
if [ -n "$SITE_HOOK" ] && ! hook_path_ok "$SITE_HOOK"; then
    log "SITE_RELEASE_HOOK must be a path inside the repository (not absolute, no ..): ${SITE_HOOK}; site hook off"
    alert "${PRODUCT} site release hook OFF: SITE_RELEASE_HOOK is not a path inside the repository"
    SITE_HOOK=""
fi
if [ -n "$PRE_HOOK" ] && ! hook_path_ok "$PRE_HOOK"; then
    log "PRE_DEPLOY_HOOK must be a path inside the repository (not absolute, no ..): ${PRE_HOOK}"
    exit 1
fi
render_policy || { log "could not write ${SCRIPT_DIR}/ci-policy.json"; exit 1; }
ensure_clone
# The empty tree's id in this clone's hash (SHA-1 or SHA-256), for app_changed.
EMPTY_TREE="$(git -C "$SRC" hash-object -t tree /dev/null)"
log "watching ${REPO}@${BRANCH} every ${POLL}s"
# A grace period that is not a number would make every site check's test
# fail quietly; fall back to the default and say so. Without a release
# marker the grace period is never used.
if [ -n "$SITE_URL" ]; then
    [[ "$SITE_GRACE" =~ ^[0-9]+$ ]] || { log "DEPLOY_SITE_GRACE_SECONDS is not a number (${SITE_GRACE}); using 1500"; SITE_GRACE=1500; }
else
    log "no site marker configured"
fi
# Say at startup whether alerts will work, not only when one is needed.
if [ -n "$ALERT_URL" ]; then
    if alert_url_ok; then log "deploy alerts on"; else log "DEPLOY_ALERT_URL has unexpected characters; alerts off"; fi
fi
while true; do
    check_ci
    check_site
    if sha=$(latest_sha) && [ -n "$sha" ] && [ "$sha" != "null" ]; then
        current="$(cat "$STATE" 2>/dev/null || true)"
        if [ "$sha" != "$current" ]; then
            if ci_green "$sha"; then
                changed=0
                app_changed "$current" "$sha" || changed=$?
                if [ "$changed" -eq 0 ]; then
                    deploy "$sha" || {
                        log "deploy of ${sha} FAILED at ${FAILED_STEP} - will retry next tick"
                        notify_failure "$sha"
                    }
                elif [ "$changed" -eq 2 ]; then
                    log "deploy of ${sha} FAILED at ${FAILED_STEP} - will retry next tick"
                    notify_failure "$sha"
                else
                    log "sha ${sha} changes no app code - recording without redeploy"
                    if echo "$sha" 2>/dev/null >"$STATE"; then
                        notify_no_app_change "$sha"
                        mark_site_pending "$sha"
                    else
                        log "could not write ${STATE} - will retry next tick"
                    fi
                fi
            else
                log "sha ${sha} waiting on CI"
            fi
        fi
    else
        log "could not read latest sha - retrying"
    fi
    # Last in the tick: this tick's deploy is over, and the clone is at the
    # sha it handled (a sha recorded without a deploy changes none of these).
    # A report that cannot be made must never stop the loop.
    check_self || true
    sleep "$POLL"
done
