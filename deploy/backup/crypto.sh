#!/bin/sh
# Source-only backup encryption API. No configuration is read until a function
# is called. Keep format-specific validation and legacy opt-ins in the caller.
#
# The OUTFILE.hmac sidecar has one of two forms, under the same derived key:
#   v2 <64 hex>  HMAC over the line backup-crypto-v2, a line with the file's
#                base name, then the ciphertext: it authenticates the bytes and
#                the name (and the date the name carries).
#   <64 hex>     Legacy. HMAC over the ciphertext alone: it authenticates the
#                bytes only, so a renamed dump still verifies.
# Authentication refuses a file that does not start with Salted__, so a v2 input
# never equals a legacy input, and a v2 sidecar with its marker removed cannot
# pass the legacy check, even over a file holding that v2 input.

_backup_crypto_require() {
    set -eu
    command -v openssl >/dev/null 2>&1 || { echo 'backup crypto: openssl is required' >&2; return 1; }
    : "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set}"
    : "${BACKUP_HMAC_SALT:?BACKUP_HMAC_SALT must be set}"
    [ "$(printf %s "$BACKUP_HMAC_SALT" | wc -c | tr -d ' ')" -eq 8 ] || {
        echo 'backup crypto: BACKUP_HMAC_SALT must be exactly 8 bytes' >&2
        return 1
    }
}

# One line of a v2 MAC input. A newline could move bytes between the name and
# the ciphertext and keep the same input, so one is refused on both sides.
_backup_crypto_check_name() {
    case "$1" in
        ''|*/*|*'
'*) echo 'backup crypto: a bound name must be a file name, with no / or newline' >&2; return 1 ;;
    esac
}

# _backup_crypto_hmac FILE: the legacy MAC. _backup_crypto_hmac FILE NAME: the
# v2 MAC, which binds FILE's bytes to NAME.
_backup_crypto_hmac() {
    # Existing derivation and OpenSSL PBKDF2 defaults; do not change the salt,
    # digest or iteration count without a separately designed format migration.
    derived=$(openssl enc -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -S "$(printf '%s' "$BACKUP_HMAC_SALT" | od -An -tx1 | tr -d ' \n')" -P) || return 1
    key=$(printf '%s\n' "$derived" | sed -n 's/^key=//p')
    case "$key" in ''|*[!0-9A-Fa-f]*) echo 'backup crypto: could not derive the MAC key' >&2; return 1 ;; esac
    [ "${#key}" -eq 64 ] || { echo 'backup crypto: could not derive the MAC key' >&2; return 1; }
    # The derived MAC key is an internal openssl argument, as in the old code.
    # The passphrase itself is always read from the environment, never argv.
    if [ "$#" -eq 2 ]; then
        # A pipe's status is openssl's alone, and POSIX sh has no pipefail: the
        # marker, beside FILE in the caller's private folder, proves cat finished.
        digest=$({ printf 'backup-crypto-v2\n%s\n' "$2"; cat "$1" && : >"$1.read"; } | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$key" -r) || return 1
        [ -e "$1.read" ] || { echo 'backup crypto: could not read the ciphertext' >&2; return 1; }
    else
        digest=$(openssl dgst -sha256 -mac HMAC -macopt "hexkey:$key" -r "$1") || return 1
    fi
    digest=${digest%% *}
    case "$digest" in ''|*[!0-9a-f]*) return 1 ;; esac
    [ "${#digest}" -eq 64 ] || return 1
    printf '%s\n' "$digest"
}

_backup_crypto_authenticate() {
    # Verify a private snapshot: a later change to the source must not become
    # the bytes decrypted after an earlier successful integrity check. A v2 MAC
    # is checked under the base name the caller gave, never the snapshot's name.
    cipher=$1
    case "$cipher" in /*) ;; *) cipher="$PWD/$cipher" ;; esac
    for file in "$cipher" "$cipher.hmac"; do
        [ -f "$file" ] && [ ! -L "$file" ] || {
            echo 'backup crypto: ciphertext and .hmac must be plain files' >&2
            return 1
        }
    done
    cp -P "$cipher" "$2/ciphertext" || return 1
    cp -P "$cipher.hmac" "$2/sidecar" || return 1
    [ -f "$2/ciphertext" ] && [ ! -L "$2/ciphertext" ] && [ -f "$2/sidecar" ] && [ ! -L "$2/sidecar" ] || return 1
    expected=$(cat "$2/sidecar") || return 1
    case "$expected" in
        'v2 '*) sidecar_form=v2; expected=${expected#v2 } ;;
        # Old dumps keep the tolerance they were written under: whitespace goes.
        *) sidecar_form=legacy; expected=$(tr -d '[:space:]' <"$2/sidecar") || return 1 ;;
    esac
    case "$expected" in ''|*[!0-9a-f]*) echo 'backup crypto: malformed .hmac sidecar' >&2; return 1 ;; esac
    [ "${#expected}" -eq 64 ] || { echo 'backup crypto: malformed .hmac sidecar' >&2; return 1; }
    if [ "$sidecar_form" = v2 ]; then
        # v2 is exact. The command substitution above dropped every NUL and the
        # final newlines, and left 67 bytes: at 68 bytes one was dropped, and a
        # newline last means that was it. Read through od, as $(tail) drops a NUL.
        [ "$(wc -c <"$2/sidecar" | tr -d ' ')" -eq 68 ] && [ "$(tail -c 1 "$2/sidecar" | od -An -tx1 | tr -d ' \n')" = 0a ] || {
            echo 'backup crypto: malformed .hmac sidecar' >&2
            return 1
        }
        bound_name=${cipher##*/}
        _backup_crypto_check_name "$bound_name" || return 1
        computed=$(_backup_crypto_hmac "$2/ciphertext" "$bound_name") || return 1
        [ "$computed" = "$expected" ] || {
            echo 'backup crypto: ciphertext or file name does not match its .hmac sidecar (modified, truncated, renamed, wrong passphrase or salt)' >&2
            return 1
        }
    else
        computed=$(_backup_crypto_hmac "$2/ciphertext") || return 1
        [ "$computed" = "$expected" ] || {
            echo 'backup crypto: ciphertext does not match its .hmac sidecar (modified, truncated, wrong passphrase or salt)' >&2
            return 1
        }
    fi
    # openssl enc -salt starts every output with this; a v2 MAC input never does.
    [ "$(head -c 8 "$2/ciphertext")" = Salted__ ] || {
        echo 'backup crypto: ciphertext does not start with Salted__, so it is not an openssl enc file' >&2
        return 1
    }
}

# Reads arbitrary stdin, writes OUTFILE and OUTFILE.hmac once. The ciphertext's
# final name is the completion marker; a caller must also check its producer's
# exit status before promoting a transient OUTFILE to an application artifact.
# With FINAL_NAME the sidecar is v2, bound to that name, so a transient OUTFILE
# verifies only once it has its final name. Without it the sidecar is legacy.
backup_encrypt() (
    set +x
    set -eu
    umask 077
    { [ "$#" -eq 1 ] || [ "$#" -eq 2 ]; } && [ -n "$1" ] || { echo 'usage: backup_encrypt OUTFILE [FINAL_NAME]' >&2; exit 2; }
    [ "$#" -eq 1 ] || _backup_crypto_check_name "$2" || exit 2
    _backup_crypto_require || exit 1
    out=$1
    [ ! -e "$out" ] && [ ! -L "$out" ] && [ ! -e "$out.hmac" ] && [ ! -L "$out.hmac" ] || {
        echo 'backup crypto: output already exists' >&2
        exit 1
    }
    work=$(mktemp -d "$(dirname "$out")/.backup-crypto.XXXXXX") || exit 1
    complete=0
    trap 'if [ "$complete" -eq 0 ]; then
        if [ "$out" -ef "$work/ciphertext" ]; then rm -f "$out"; fi
        if [ "$out.hmac" -ef "$work/sidecar" ]; then rm -f "$out.hmac"; fi
    fi; rm -rf "$work"' EXIT
    trap 'exit 1' HUP INT TERM
    openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out "$work/ciphertext" || exit 1
    if [ "$#" -eq 2 ]; then
        mac=$(_backup_crypto_hmac "$work/ciphertext" "$2") || exit 1
        printf 'v2 %s\n' "$mac" >"$work/sidecar" || exit 1
    else
        _backup_crypto_hmac "$work/ciphertext" >"$work/sidecar" || exit 1
    fi
    # Same-filesystem hard links do not overwrite a competing immutable output.
    ln "$work/sidecar" "$out.hmac" || exit 1
    ln "$work/ciphertext" "$out" || exit 1
    complete=1
)

# Authentication only; no plaintext is produced.
backup_verify() (
    set +x
    set -eu
    umask 077
    [ "$#" -eq 1 ] || { echo 'usage: backup_verify FILE' >&2; exit 2; }
    _backup_crypto_require || exit 1
    work=$(mktemp -d "${TMPDIR:-/tmp}/backup-crypto.XXXXXX") || exit 1
    trap 'rm -rf "$work"' EXIT
    trap 'exit 1' HUP INT TERM
    _backup_crypto_authenticate "$1" "$work" || exit 1
)

# Authenticates and completely decrypts a private snapshot before emitting any
# plaintext to stdout. The caller owns decompression and application checks.
# --warn-legacy: say on stderr when the snapshot's sidecar has the legacy form.
# --require-v2: refuse, with no plaintext, when it has that form.
# Read here because the sidecar on disk may have changed since the snapshot.
backup_decrypt() (
    set +x
    set -eu
    umask 077
    case "$#:${2:-}" in
        1:|2:--warn-legacy|2:--require-v2) ;;
        *) echo 'usage: backup_decrypt FILE [--warn-legacy|--require-v2]' >&2; exit 2 ;;
    esac
    _backup_crypto_require || exit 1
    work=$(mktemp -d "${TMPDIR:-/tmp}/backup-crypto.XXXXXX") || exit 1
    trap 'rm -rf "$work"' EXIT
    trap 'exit 1' HUP INT TERM
    _backup_crypto_authenticate "$1" "$work" || exit 1
    if [ "${2:-}" = --require-v2 ] && [ "$sidecar_form" = legacy ]; then
        echo 'backup crypto: this restore requires a v2 .hmac sidecar; a legacy one does not bind the file name or its date' >&2
        exit 1
    fi
    if [ "${2:-}" = --warn-legacy ] && [ "$sidecar_form" = legacy ]; then
        echo 'WARNING: old format .hmac sidecar: the bytes are authenticated, but the file name and its date are not' >&2
    fi
    openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$work/ciphertext" -out "$work/plaintext" || exit 1
    cat "$work/plaintext"
)
