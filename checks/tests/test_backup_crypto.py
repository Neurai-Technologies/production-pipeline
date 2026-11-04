"""The shared source-only API writes the name-bound v2 sidecar and still reads the historical format."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import LIBRARY_ROOT
from tests.shell import fake

CRYPTO = LIBRARY_ROOT / "deploy" / "backup" / "crypto.sh"
PASSPHRASE = "disposable encryption exercise"
SALT = "fakesalt"
DATA = b"arbitrary archive bytes\x00\xff\n" * 200
# Two implementations where the machine has both: LibreSSL and OpenSSL 3 on a Mac.
OPENSSLS = [str(p) for p in (Path("/usr/bin/openssl"), Path("/opt/homebrew/bin/openssl")) if p.exists()]


def invoke(
    function: str, *args: Path | str, data: bytes | None = None, **env: str
) -> subprocess.CompletedProcess:
    """Call the actual source-only module, without any database configuration."""
    return subprocess.run(
        ["bash", "-c", '. "$1"; shift; "$@"', "crypto-test", str(CRYPTO), function, *map(str, args)],
        input=data,
        env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE, "BACKUP_HMAC_SALT": SALT, **env},
        capture_output=True,
        check=False,
        timeout=15,
    )


def legacy_pair(path: Path, data: bytes = DATA) -> None:
    """Build the old format independently of the new helper, with fixture secrets."""
    result = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "env:BACKUP_PASSPHRASE"],
        input=data,
        env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE},
        capture_output=True,
        check=True,
    )
    path.write_bytes(result.stdout)
    # Old mac_key() uses the first 32 bytes OpenSSL derives for AES-256-CBC.
    key = hashlib.pbkdf2_hmac("sha256", PASSPHRASE.encode(), SALT.encode(), 10000, dklen=48)[:32]
    Path(f"{path}.hmac").write_text(hmac.new(key, result.stdout, "sha256").hexdigest() + "\n")


def mac_key() -> bytes:
    """The derived MAC key, independently of the helper."""
    return hashlib.pbkdf2_hmac("sha256", PASSPHRASE.encode(), SALT.encode(), 10000, dklen=48)[:32]


def v2_mac(name: bytes, ciphertext: bytes) -> str:
    """The v2 MAC, independently of the helper."""
    return hmac.new(mac_key(), b"backup-crypto-v2\n" + name + b"\n" + ciphertext, "sha256").hexdigest()


def on_openssl(binary: str, folder: Path) -> str:
    """A PATH on which `openssl` is exactly this binary."""
    fake(folder, "openssl", f'exec "{binary}" "$@"\n')
    return f"{folder}:{os.environ['PATH']}"


def v2_pair(path: Path, name: str | None = None, **env: str) -> None:
    """Encrypt through the helper, with a v2 sidecar bound to `name` or to the path's own name."""
    encrypted = invoke("backup_encrypt", path, name or path.name, data=DATA, **env)
    assert encrypted.returncode == 0, encrypted.stderr


def rename_pair(path: Path, name: str) -> Path:
    """Move a dump and its sidecar together, as an attacker with the folder could."""
    target = path.with_name(name)
    path.rename(target)
    Path(f"{path}.hmac").rename(f"{target}.hmac")
    return target


@pytest.mark.parametrize("binary", OPENSSLS)
def test_v2_round_trip_under_the_final_name(tmp_path: Path, binary: str) -> None:
    path_env = on_openssl(binary, tmp_path / "bin")
    final = "fakedump-20260817-020000.sql.gz.enc"
    part = tmp_path / f"{final}.part"
    v2_pair(part, final, PATH=path_env)
    assert invoke("backup_verify", part, PATH=path_env, TMPDIR=str(tmp_path)).returncode != 0, (
        "the .part name is not the bound one"
    )
    path = rename_pair(part, final)
    assert Path(f"{path}.hmac").read_bytes() == f"v2 {v2_mac(final.encode(), path.read_bytes())}\n".encode()
    assert Path(f"{path}.hmac").stat().st_mode & 0o777 == 0o600
    assert invoke("backup_verify", path, PATH=path_env, TMPDIR=str(tmp_path)).returncode == 0
    relative = Path(os.path.relpath(path))
    assert invoke("backup_verify", relative, PATH=path_env, TMPDIR=str(tmp_path)).returncode == 0
    decoded = invoke("backup_decrypt", path, PATH=path_env, TMPDIR=str(tmp_path))
    assert decoded.returncode == 0 and decoded.stdout == DATA, decoded.stderr
    assert not list(tmp_path.glob("backup-crypto.*"))


@pytest.mark.parametrize("verifier", OPENSSLS)
@pytest.mark.parametrize("writer", OPENSSLS)
def test_a_v2_sidecar_from_one_openssl_verifies_on_the_other(
    tmp_path: Path, writer: str, verifier: str
) -> None:
    path = tmp_path / "archive.enc"
    v2_pair(path, PATH=on_openssl(writer, tmp_path / "writer"))
    read_env = on_openssl(verifier, tmp_path / "verifier")
    assert invoke("backup_verify", path, PATH=read_env, TMPDIR=str(tmp_path)).returncode == 0
    decoded = invoke("backup_decrypt", path, PATH=read_env, TMPDIR=str(tmp_path))
    assert decoded.returncode == 0 and decoded.stdout == DATA, decoded.stderr


@pytest.mark.parametrize("binary", OPENSSLS)
def test_renamed_v2_is_refused_with_a_clear_message(tmp_path: Path, binary: str) -> None:
    path_env = on_openssl(binary, tmp_path / "bin")
    path = tmp_path / "fakedump-20260817-020000.sql.gz.enc"
    v2_pair(path, PATH=path_env)
    renamed = rename_pair(path, "fakedump-20270101-000000.sql.gz.enc")
    for function in ("backup_verify", "backup_decrypt"):
        result = invoke(function, renamed, PATH=path_env, TMPDIR=str(tmp_path))
        assert result.returncode != 0 and not result.stdout
        assert b"renamed" in result.stderr and b"does not match its .hmac sidecar" in result.stderr
    assert not list(tmp_path.glob("backup-crypto.*"))


def test_v2_with_its_marker_stripped_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "archive.enc"
    v2_pair(path)
    sidecar = Path(f"{path}.hmac")
    sidecar.write_text(sidecar.read_text().removeprefix("v2 "))
    result = invoke("backup_verify", path, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and b"does not match its .hmac sidecar" in result.stderr


def test_legacy_sidecar_with_a_v2_marker_added_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    sidecar = Path(f"{path}.hmac")
    sidecar.write_text(f"v2 {sidecar.read_text()}")
    result = invoke("backup_verify", path, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and b"does not match its .hmac sidecar" in result.stderr


def test_a_renamed_legacy_dump_still_verifies(tmp_path: Path) -> None:
    """What the legacy form does not protect: its name, and so the date in it."""
    path = tmp_path / "fakedump-20260817-020000.sql.gz.enc"
    legacy_pair(path)
    renamed = rename_pair(path, "fakedump-20270101-000000.sql.gz.enc")
    assert invoke("backup_verify", renamed, TMPDIR=str(tmp_path)).returncode == 0


@pytest.mark.parametrize("binary", OPENSSLS)
def test_one_argument_encrypt_writes_the_legacy_form_and_round_trips(tmp_path: Path, binary: str) -> None:
    path_env = on_openssl(binary, tmp_path / "bin")
    path = tmp_path / "archive.enc"
    encrypted = invoke("backup_encrypt", path, data=DATA, PATH=path_env)
    assert encrypted.returncode == 0, encrypted.stderr
    assert (
        Path(f"{path}.hmac").read_bytes()
        == f"{hmac.new(mac_key(), path.read_bytes(), 'sha256').hexdigest()}\n".encode()
    )
    decoded = invoke("backup_decrypt", path, PATH=path_env, TMPDIR=str(tmp_path))
    assert decoded.returncode == 0 and decoded.stdout == DATA, decoded.stderr


@pytest.mark.parametrize(
    "shape",
    [
        "v2",
        "v2 ",
        "v2 \n",
        "v2\n",
        "v3 {mac}\n",
        "V2 {mac}\n",
        "v2  {mac}\n",
        "v2\t{mac}\n",
        " v2 {mac}\n",
        "v2{mac}\n",
        "v2 {mac}",
        "v2 {mac}\n\n",
        "v2 {mac}\r\n",
        "v2 {mac} \n",
        "v2 {mac}\nv2 {mac}\n",
        "v2 {short}\n",
        "v2 {upper}\n",
    ],
)
def test_malformed_v2_sidecars_are_refused(tmp_path: Path, shape: str) -> None:
    """Each one carries the right MAC, so the form alone can be why it is refused."""
    path = tmp_path / "archive.enc"
    v2_pair(path)
    mac = v2_mac(path.name.encode(), path.read_bytes())
    Path(f"{path}.hmac").write_bytes(shape.format(mac=mac, short=mac[:63], upper=mac.upper()).encode())
    result = invoke("backup_decrypt", path, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and not result.stdout
    assert b"malformed .hmac sidecar" in result.stderr


@pytest.mark.parametrize("shape", ["{short}\n", "{upper}\n", "{mac}0\n", "{mac}\n{mac}\n"])
def test_malformed_legacy_sidecars_are_refused(tmp_path: Path, shape: str) -> None:
    """Each one carries the right MAC, so the form alone can be why it is refused."""
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    mac = Path(f"{path}.hmac").read_text().strip()
    Path(f"{path}.hmac").write_bytes(shape.format(mac=mac, short=mac[:63], upper=mac.upper()).encode())
    result = invoke("backup_decrypt", path, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and not result.stdout
    assert b"malformed .hmac sidecar" in result.stderr


@pytest.mark.parametrize("shape", ["{mac}", "{mac}\n\n", "{mac}\r\n", " {mac}\n", "{mac} \n", "\t{mac}\t\n"])
def test_a_legacy_sidecar_keeps_its_whitespace_tolerance(tmp_path: Path, shape: str) -> None:
    """Old dumps stay restorable: whitespace is stripped before the legacy form is read, as it always was."""
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    mac = Path(f"{path}.hmac").read_text().strip()
    Path(f"{path}.hmac").write_bytes(shape.format(mac=mac).encode())
    decoded = invoke("backup_decrypt", path, TMPDIR=str(tmp_path))
    assert decoded.returncode == 0 and decoded.stdout == DATA, decoded.stderr


@pytest.mark.parametrize("name", ["", "a/b.enc", "/b.enc", "b.enc/", "a\nb.enc", "b.enc\n"])
def test_a_name_with_a_slash_or_newline_is_refused(tmp_path: Path, name: str) -> None:
    result = invoke("backup_encrypt", tmp_path / "new.enc", name, data=DATA)
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []


def test_a_file_name_with_a_newline_cannot_borrow_ciphertext_bytes(tmp_path: Path) -> None:
    """The MAC input of name `a` and bytes `Salted__\\nrest` is also that of name `a\\nSalted__` and bytes `rest`."""
    ciphertext = b"Salted__\nrest"
    mac = v2_mac(b"a", ciphertext)
    honest = tmp_path / "a"
    honest.write_bytes(ciphertext)
    Path(f"{honest}.hmac").write_text(f"v2 {mac}\n")
    assert invoke("backup_verify", honest, TMPDIR=str(tmp_path)).returncode == 0
    shifted = tmp_path / "a\nSalted__"
    shifted.write_bytes(b"rest")
    Path(f"{shifted}.hmac").write_text(f"v2 {mac}\n")
    assert v2_mac(b"a\nSalted__", b"rest") == mac, "the fixture must collide, or it proves nothing"
    result = invoke("backup_verify", shifted, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and b"newline" in result.stderr


def test_v2_and_legacy_sidecars_never_verify_for_one_another(tmp_path: Path) -> None:
    """The same bytes under the same name: each form's MAC, written in the other form."""
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    legacy = Path(f"{path}.hmac").read_text().strip()
    v2 = v2_mac(path.name.encode(), path.read_bytes())
    assert legacy != v2
    Path(f"{path}.hmac").write_text(f"v2 {v2}\n")
    assert invoke("backup_verify", path, TMPDIR=str(tmp_path)).returncode == 0
    Path(f"{path}.hmac").write_text(f"{v2}\n")
    assert invoke("backup_verify", path, TMPDIR=str(tmp_path)).returncode != 0
    Path(f"{path}.hmac").write_text(f"v2 {legacy}\n")
    assert invoke("backup_verify", path, TMPDIR=str(tmp_path)).returncode != 0
    Path(f"{path}.hmac").write_text(f"{legacy}\n")
    assert invoke("backup_verify", path, TMPDIR=str(tmp_path)).returncode == 0


def test_a_v2_input_written_as_a_file_cannot_pass_as_legacy(tmp_path: Path) -> None:
    """The v2 MAC input as file bytes, the bare v2 MAC as a legacy sidecar: the MAC holds, the header cannot."""
    honest = tmp_path / "honest.enc"
    v2_pair(honest)
    mac = Path(f"{honest}.hmac").read_text().removeprefix("v2 ").strip()
    forged = tmp_path / "forged.enc"
    forged.write_bytes(b"backup-crypto-v2\n" + honest.name.encode() + b"\n" + honest.read_bytes())
    Path(f"{forged}.hmac").write_text(f"{mac}\n")
    assert hmac.new(mac_key(), forged.read_bytes(), "sha256").hexdigest() == mac, (
        "the fixture must verify as legacy"
    )
    verified = invoke("backup_verify", forged, TMPDIR=str(tmp_path))
    assert verified.returncode != 0 and b"does not start with Salted__" in verified.stderr, verified.stderr
    decrypted = invoke("backup_decrypt", forged, TMPDIR=str(tmp_path))
    assert decrypted.returncode != 0 and not decrypted.stdout
    assert b"does not start with Salted__" in decrypted.stderr, decrypted.stderr


@pytest.mark.parametrize("head", [b"", b"Salted_", b"Salted_\n", b"salted__", b"Salt\x00ed__"])
def test_a_snapshot_without_the_exact_header_is_refused(tmp_path: Path, head: bytes) -> None:
    path = tmp_path / "archive.enc"
    path.write_bytes(head)
    Path(f"{path}.hmac").write_text(hmac.new(mac_key(), head, "sha256").hexdigest() + "\n")
    result = invoke("backup_verify", path, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and b"does not start with Salted__" in result.stderr, result.stderr


def test_only_a_passing_legacy_sidecar_warns_and_only_when_asked(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.enc"
    legacy_pair(legacy)
    asked = invoke("backup_decrypt", legacy, "--warn-legacy", TMPDIR=str(tmp_path))
    assert asked.returncode == 0 and asked.stdout == DATA, asked.stderr
    assert asked.stderr.count(b"\n") == 1 and b"old format" in asked.stderr
    assert not invoke("backup_decrypt", legacy, TMPDIR=str(tmp_path)).stderr
    current = tmp_path / "current.enc"
    v2_pair(current)
    assert not invoke("backup_decrypt", current, "--warn-legacy", TMPDIR=str(tmp_path)).stderr
    Path(f"{legacy}.hmac").write_text("0" * 64 + "\n")
    refused = invoke("backup_decrypt", legacy, "--warn-legacy", TMPDIR=str(tmp_path))
    assert refused.returncode != 0 and not refused.stdout and b"old format" not in refused.stderr
    assert invoke("backup_decrypt", legacy, "--other", TMPDIR=str(tmp_path)).returncode == 2


def test_the_warning_reads_the_snapshot_not_a_sidecar_changed_afterwards(tmp_path: Path) -> None:
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    real = shutil.which("openssl")
    assert real
    fake(
        tmp_path / "bin",
        "openssl",
        f'case " $* " in *" dgst "*) printf "v2 %064d\\n" 0 >"{path}.hmac" ;; esac\nexec "{real}" "$@"\n',
    )
    path_env = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    result = invoke("backup_decrypt", path, "--warn-legacy", PATH=path_env, TMPDIR=str(tmp_path))
    assert result.returncode == 0 and result.stdout == DATA, result.stderr
    assert Path(f"{path}.hmac").read_text().startswith("v2 "), "the fixture must change the sidecar on disk"
    assert b"old format" in result.stderr


def test_require_v2_refuses_a_passing_legacy_sidecar_and_accepts_v2(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.enc"
    legacy_pair(legacy)
    refused = invoke("backup_decrypt", legacy, "--require-v2", TMPDIR=str(tmp_path))
    assert refused.returncode != 0 and not refused.stdout
    assert b"requires a v2 .hmac sidecar" in refused.stderr and b"old format" not in refused.stderr
    current = tmp_path / "current.enc"
    v2_pair(current)
    accepted = invoke("backup_decrypt", current, "--require-v2", TMPDIR=str(tmp_path))
    assert accepted.returncode == 0 and accepted.stdout == DATA and not accepted.stderr
    both = invoke("backup_decrypt", current, "--warn-legacy", "--require-v2", TMPDIR=str(tmp_path))
    assert both.returncode == 2


def test_require_v2_reads_the_snapshot_not_a_sidecar_changed_afterwards(tmp_path: Path) -> None:
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    real = shutil.which("openssl")
    assert real
    fake(
        tmp_path / "bin",
        "openssl",
        f'case " $* " in *" dgst "*) printf "v2 %064d\\n" 0 >"{path}.hmac" ;; esac\nexec "{real}" "$@"\n',
    )
    path_env = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    result = invoke("backup_decrypt", path, "--require-v2", PATH=path_env, TMPDIR=str(tmp_path))
    assert Path(f"{path}.hmac").read_text().startswith("v2 "), "the fixture must change the sidecar on disk"
    assert result.returncode != 0 and not result.stdout
    assert b"requires a v2 .hmac sidecar" in result.stderr


def test_import_is_side_effect_free_and_requires_no_settings(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", "-c", '. "$1"', "import-test", str(CRYPTO)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert list(tmp_path.iterdir()) == []


def test_old_ciphertext_decrypts_and_new_ciphertext_uses_old_algorithm(tmp_path: Path) -> None:
    old = tmp_path / "old.enc"
    legacy_pair(old)
    assert invoke("backup_verify", old, TMPDIR=str(tmp_path)).returncode == 0
    decoded = invoke("backup_decrypt", old, TMPDIR=str(tmp_path))
    assert decoded.returncode == 0 and decoded.stdout == DATA, decoded.stderr
    new = tmp_path / "new file.enc"
    encrypted = invoke("backup_encrypt", new, data=DATA)
    assert encrypted.returncode == 0, encrypted.stderr
    key = hashlib.pbkdf2_hmac("sha256", PASSPHRASE.encode(), SALT.encode(), 10000, dklen=48)[:32]
    assert Path(f"{new}.hmac").read_text().strip() == hmac.new(key, new.read_bytes(), "sha256").hexdigest()
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            "env:BACKUP_PASSPHRASE",
            "-in",
            str(new),
        ],
        env={**os.environ, "BACKUP_PASSPHRASE": PASSPHRASE},
        capture_output=True,
        check=True,
    )
    assert result.stdout == DATA
    assert new.stat().st_mode & 0o777 == 0o600
    assert Path(f"{new}.hmac").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*crypto*"))


@pytest.mark.parametrize(
    "damage", ["missing", "empty", "malformed", "wrong_mac", "modified", "truncated", "password", "salt"]
)
def test_bad_authentication_emits_no_plaintext_or_decrypt_call(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    sidecar = Path(f"{path}.hmac")
    env = {}
    if damage == "missing":
        sidecar.unlink()
    elif damage == "empty":
        sidecar.write_text("")
    elif damage == "malformed":
        sidecar.write_text("z" * 64 + "\n")
    elif damage == "wrong_mac":
        sidecar.write_text("0" * 64 + "\n")
    elif damage == "modified":
        path.write_bytes(path.read_bytes()[:30] + b"changed" + path.read_bytes()[37:])
    elif damage == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == "password":
        env["BACKUP_PASSPHRASE"] = "wrong fixture"
    elif damage == "salt":
        env["BACKUP_HMAC_SALT"] = "othersal"
    called = tmp_path / "decrypt-called"
    real = shutil.which("openssl")
    assert real
    fake(
        tmp_path / "bin",
        "openssl",
        f'case " $* " in *" -d "*) touch "{called}" ;; esac\nexec "{real}" "$@"\n',
    )
    result = invoke(
        "backup_decrypt", path, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", TMPDIR=str(tmp_path), **env
    )
    assert result.returncode != 0 and not result.stdout, result.stderr
    assert not called.exists(), "decryption was invoked before authentication succeeded"
    assert not list(tmp_path.glob("backup-crypto.*"))


@pytest.mark.parametrize("function", ["backup_encrypt", "backup_verify", "backup_decrypt"])
@pytest.mark.parametrize("setting,value", [("BACKUP_PASSPHRASE", ""), ("BACKUP_HMAC_SALT", "short")])
def test_bad_configuration_fails_closed(tmp_path: Path, function: str, setting: str, value: str) -> None:
    result = invoke(function, tmp_path / "new.enc", data=DATA, **{setting: value})
    assert result.returncode != 0 and not result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["encrypt", "mac", "signal"])
def test_failed_or_interrupted_encrypt_publishes_no_pair(tmp_path: Path, failure: str) -> None:
    real = shutil.which("openssl")
    assert real
    if failure == "mac":
        body = f'case " $* " in *" dgst "*) exit 1 ;; esac\nexec "{real}" "$@"\n'
    else:
        body = 'if [ "$1" = enc ]; then\n  while [ "$1" != -out ]; do shift; done\n  printf partial >"$2"\n'
        body += '  kill -TERM "$PPID"\n' if failure == "signal" else ""
        body += "  exit 1\nfi\n"
    fake(tmp_path / "bin", "openssl", body)
    result = invoke(
        "backup_encrypt", tmp_path / "new.enc", data=DATA, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    )
    assert result.returncode != 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin"]


def test_a_read_error_while_macing_publishes_no_pair(tmp_path: Path) -> None:
    """The pipe's status is openssl's: without its own check, a failed cat would publish a wrong sidecar."""
    fake(tmp_path / "bin", "cat", "exit 1\n")
    out = tmp_path / "new.enc"
    result = invoke(
        "backup_encrypt", out, out.name, data=DATA, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    )
    assert result.returncode != 0 and b"could not read the ciphertext" in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin"]


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "kept.enc"
    path.write_bytes(b"kept")
    result = invoke("backup_encrypt", path, data=DATA)
    assert result.returncode != 0 and path.read_bytes() == b"kept"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kept.enc"]


def test_decrypt_uses_the_authenticated_snapshot_not_a_changed_source(tmp_path: Path) -> None:
    path = tmp_path / "archive.enc"
    legacy_pair(path)
    real = shutil.which("openssl")
    assert real
    fake(
        tmp_path / "bin",
        "openssl",
        f'case " $* " in *" -d "*) printf changed >"{path}" ;; esac\nexec "{real}" "$@"\n',
    )
    result = invoke(
        "backup_decrypt", path, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", TMPDIR=str(tmp_path)
    )
    assert result.returncode == 0 and result.stdout == DATA, result.stderr
    assert path.read_bytes() == b"changed"
    assert not list(tmp_path.glob("backup-crypto.*"))


@pytest.mark.parametrize("publication", ["sidecar", "ciphertext"])
def test_signal_immediately_after_publication_removes_only_our_links(
    tmp_path: Path, publication: str
) -> None:
    fake(
        tmp_path / "bin",
        "ln",
        f'/bin/ln "$@" || exit $?\ncase "$1" in */{publication}) kill -TERM "$PPID" ;; esac\n',
    )
    result = invoke(
        "backup_encrypt", tmp_path / "new.enc", data=DATA, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    )
    assert result.returncode != 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin"]


def test_publication_failure_does_not_remove_a_competing_output(tmp_path: Path) -> None:
    fake(
        tmp_path / "bin",
        "ln",
        '/bin/ln "$@" || exit $?\ncase "$1" in */sidecar) printf competing >"${2%.hmac}" ;; esac\n',
    )
    out = tmp_path / "new.enc"
    result = invoke("backup_encrypt", out, data=DATA, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    assert result.returncode != 0, result.stderr
    assert out.read_bytes() == b"competing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin", "new.enc"]


def test_authenticated_but_undecryptable_ciphertext_emits_no_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "bad-padding.enc"
    legacy_pair(path)
    truncated = path.read_bytes()[:-1]
    path.write_bytes(truncated)
    key = hashlib.pbkdf2_hmac("sha256", PASSPHRASE.encode(), SALT.encode(), 10000, dklen=48)[:32]
    Path(f"{path}.hmac").write_text(hmac.new(key, truncated, "sha256").hexdigest() + "\n")
    assert invoke("backup_verify", path, TMPDIR=str(tmp_path)).returncode == 0
    result = invoke("backup_decrypt", path, TMPDIR=str(tmp_path))
    assert result.returncode != 0 and not result.stdout
    assert not list(tmp_path.glob("backup-crypto.*"))
