"""Every deploy and every pipeline failure must reach a person; a lost alert must never fail a deploy.

A deployer that only logs "FAILED - will retry next tick" leaves a broken
release broken until someone happens to read the container log. The
functions under test are lifted from the real deploy loop and run under bash
with a fake curl on PATH, after the fake product.settings the deployer's
env_file would supply.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import LIBRARY_ROOT, ProductSettings
from tests.shell import fake, git_env, lift
from tests.test_deploy_hardening import block

LOOP = LIBRARY_ROOT / "deploy" / "deployer" / "deploy-loop.sh"
DOCKERFILE = LOOP.parent / "Dockerfile"
HOOK = "deploy/site-release.sh"
NAMES = (
    "alert_url_ok",
    "alert",
    "notify_failure",
    "forget",
    "subject_of",
    "notify_deployed",
    "post_deploy_status",
    "notify_no_app_change",
    "ci_failures",
    "check_ci",
    "mark_site_pending",
    "check_site",
    "run_site_hook",
    "check_self",
)


def functions() -> str:
    return "".join(lift(LOOP, name) for name in NAMES)


def baked_line() -> str:
    lines = [line for line in LOOP.read_text(encoding="utf-8").splitlines() if line.startswith("BAKED=(")]
    assert len(lines) == 1 and lines[0].endswith(")"), "BAKED moved; update this test"
    return lines[0]


def baked_names() -> list[str]:
    return shlex.split(baked_line()[len("BAKED=(") : -1])


@pytest.fixture
def env(product_settings: Callable[..., ProductSettings]) -> ProductSettings:
    return product_settings()


@pytest.fixture
def harness(tmp_path: Path, env: ProductSettings) -> dict[str, Path]:
    calls = tmp_path / "curl-calls"
    # Records argv on one line and stdin on the next; exit status comes from a file.
    fake(
        tmp_path / "bin",
        "curl",
        f'printf "argv: %s\\n" "$*" >> "{calls}"\n'
        f'printf "stdin: %s\\n" "$(cat)" >> "{calls}"\n'
        f'cat "{tmp_path}/curl-body" 2>/dev/null\n'
        f'exit "$(cat "{tmp_path}/curl-status" 2>/dev/null || echo 0)"\n',
    )
    return {
        "bin": tmp_path / "bin",
        "env": env.path,
        "calls": calls,
        "alerted": tmp_path / "alerted-sha",
        "status": tmp_path / "curl-status",
        "body": tmp_path / "curl-body",
        "ci_alerted": tmp_path / "ci-alerted-sha",
        "site_pending": tmp_path / "site-pending",
        "site_alerted": tmp_path / "site-alerted-sha",
        "site_hooked": tmp_path / "site-hook-sha",
        "hook_calls": tmp_path / "hook-calls",
        "hook_status": tmp_path / "hook-status",
        "src": tmp_path / "src",
        "behind": tmp_path / "deployer-behind",
        "baked": tmp_path / "baked",
    }


def run(
    harness: dict[str, Path],
    commands: str,
    url: str = "https://example.invalid/bot123:abc/sendMessage?chat_id=9",
    site_hook: str = HOOK,
    site_url: str = "https://example.invalid/_release.json",
) -> str:
    prelude = (
        f". {shlex.quote(str(harness['env']))}\n"
        f'ALERT_URL={shlex.quote(url)}; ALERTED="{harness["alerted"]}"; POLL=90; FAILED_STEP="compose up"; '
        f'CI_BRANCH=ci; CI_ALERTED="{harness["ci_alerted"]}"; SITE_URL={shlex.quote(site_url)}; '
        f'SITE_GRACE=1500; SITE_PENDING="{harness["site_pending"]}"; SITE_ALERTED="{harness["site_alerted"]}"; '
        f'SITE_HOOK={shlex.quote(site_hook)}; SITE_HOOKED="{harness["site_hooked"]}"; BRANCH=main; '
        f'{baked_line()}; BEHIND="{harness["behind"]}"; BEHIND_READ=""; BEHIND_SAID=""; UNCOMPARED=""; '
        f'SCRIPT_DIR="{harness["baked"]}"; API=https://api.invalid/repos/x/y; SRC="{harness["src"]}"; '
        "GITHUB_DEPLOY_TOKEN=t; "
        'gh_curl() { curl -fsS "$@"; }; git_authed() { git "$@"; }; '
        'log() { echo "[deployer] $*"; }\n'
    )
    run_env = {**git_env(), "PATH": f"{harness['bin']}:{git_env()['PATH']}"}
    # stderr is folded in: the deployer's log is the container's combined
    # output, so a stray shell message is as visible there as a log line.
    result = subprocess.run(
        ["bash", "-c", f"{prelude}{functions()}{commands}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=run_env,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout


def calls(harness: dict[str, Path]) -> list[str]:
    return harness["calls"].read_text().splitlines() if harness["calls"].exists() else []


def alerts(harness: dict[str, Path]) -> list[str]:
    return [line for line in calls(harness) if line.startswith("argv:") and "text=" in line]


def clone_with(
    harness: dict[str, Path], count: int, hook: bool = True, hook_mode: int = 0o755, symlink: bool = False
) -> list[str]:
    """A scratch clone with `count` commits, each carrying an executable site
    release hook that records its arguments and exits with the status in a
    file (default 0). It also records whether it can see the token or the
    alert URL. As a symlink, the hook points at a real executable beside it."""
    src = harness["src"]
    src.mkdir()
    env = git_env()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True, env=env)
    if hook:
        script = src / HOOK
        script.parent.mkdir(parents=True)
        if symlink:
            script.symlink_to("real-hook.sh")
            script = script.with_name("real-hook.sh")
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s|%s\\n" "$1" "$2" >> "{harness["hook_calls"]}"\n'
            f'pwd -P > "{harness["hook_calls"]}.cwd"\n'
            f'printf "%s %s" "${{GITHUB_DEPLOY_TOKEN-unset}}" "${{DEPLOY_ALERT_URL-unset}}" > "{harness["hook_calls"]}.vars"\n'
            f'exit "$(cat "{harness["hook_status"]}" 2>/dev/null || echo 0)"\n'
        )
        script.chmod(hook_mode)
    shas = []
    for i in range(count):
        (src / "page.txt").write_text(f"v{i}\n")
        subprocess.run(["git", "add", "-A"], cwd=src, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", f"change {i}"], cwd=src, check=True, env=env)
        shas.append(
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=src, text=True, env=env).strip()
        )
    return shas


def hook_calls(harness: dict[str, Path]) -> list[str]:
    return harness["hook_calls"].read_text().splitlines() if harness["hook_calls"].exists() else []


def head(harness: dict[str, Path]) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=harness["src"], text=True, env=git_env()
    ).strip()


def test_the_first_published_sha_runs_the_hook_with_no_baseline(harness: dict[str, Path]) -> None:
    """What to do without a previous sha is the hook's call; the loop hands it an empty $2."""
    (a,) = clone_with(harness, 1)
    run(harness, f"run_site_hook {a}")
    assert hook_calls(harness) == [f"{a}|"]
    assert harness["site_hooked"].read_text().strip() == a
    assert alerts(harness) == []


def test_a_sha_that_is_not_a_sha_is_ignored_before_anything_runs(harness: dict[str, Path]) -> None:
    clone_with(harness, 1)
    harness["site_hooked"].write_text("b" * 40 + "\n")
    run(harness, "run_site_hook 'main; touch /tmp/x'; run_site_hook abc123")
    assert hook_calls(harness) == [] and calls(harness) == []
    assert harness["site_hooked"].read_text().strip() == "b" * 40


def test_a_published_sha_runs_the_hook_from_the_last_accepted_sha(harness: dict[str, Path]) -> None:
    a, b = clone_with(harness, 2)
    harness["site_hooked"].write_text(a + "\n")
    out = run(harness, f"run_site_hook {b}")
    assert hook_calls(harness) == [f"{b}|{a}"]
    assert harness["site_hooked"].read_text().strip() == b
    assert "site release hook done" in out
    assert calls(harness) == []
    assert head(harness) == b, "the hook must run from the clone checked out at the published sha"


def test_a_failed_hook_alerts_and_the_next_sha_carries_the_range(harness: dict[str, Path]) -> None:
    a, b, c = clone_with(harness, 3)
    harness["site_hooked"].write_text(a + "\n")
    harness["hook_status"].write_text("1")
    run(harness, f"run_site_hook {b}")
    assert harness["site_hooked"].read_text().strip() == a, "a failed hook must not advance the baseline"
    sent = alerts(harness)
    assert len(sent) == 1 and "site release hook FAILED" in sent[0] and a[:7] in sent[0]
    harness["hook_status"].write_text("0")
    run(harness, f"run_site_hook {c}")
    assert hook_calls(harness)[-1] == f"{c}|{a}"
    assert harness["site_hooked"].read_text().strip() == c
    assert len(alerts(harness)) == 1


def test_a_sha_already_accepted_is_not_run_again(harness: dict[str, Path]) -> None:
    (a,) = clone_with(harness, 1)
    harness["site_hooked"].write_text(a + "\n")
    run(harness, f"run_site_hook {a}")
    assert hook_calls(harness) == [] and calls(harness) == []


def test_a_corrupt_baseline_is_replaced_not_trusted(harness: dict[str, Path]) -> None:
    (a,) = clone_with(harness, 1)
    harness["site_hooked"].write_text("not a sha\n")
    run(harness, f"run_site_hook {a}")
    assert hook_calls(harness) == [f"{a}|"], "a baseline that is not a sha is handed on as none"
    assert harness["site_hooked"].read_text().strip() == a


def test_no_hook_configured_means_nothing_runs(harness: dict[str, Path]) -> None:
    (a,) = clone_with(harness, 1)
    run(harness, f"run_site_hook {a}", site_hook="")
    assert hook_calls(harness) == [] and alerts(harness) == []
    assert not harness["site_hooked"].exists()


def test_a_configured_hook_missing_at_the_sha_alerts_and_keeps_the_baseline(harness: dict[str, Path]) -> None:
    """Set but not there is a failed run, never a silent skip: the next
    published sha retries from the same baseline."""
    (a,) = clone_with(harness, 1, hook=False)
    run(harness, f"run_site_hook {a}")
    sent = alerts(harness)
    assert len(sent) == 1 and "site release hook FAILED" in sent[0] and "missing or not executable" in sent[0]
    assert not harness["site_hooked"].exists()


def test_a_configured_hook_that_is_not_executable_alerts(harness: dict[str, Path]) -> None:
    clone_with(harness, 1, hook_mode=0o644)
    run(harness, f"run_site_hook {head(harness)}")
    sent = alerts(harness)
    assert len(sent) == 1 and "missing or not executable" in sent[0]
    assert hook_calls(harness) == [] and not harness["site_hooked"].exists()


def test_the_hook_runs_from_the_clones_root(harness: dict[str, Path]) -> None:
    """So a hook's relative paths are the repository's, whatever the loop's own directory."""
    (a,) = clone_with(harness, 1)
    run(harness, f"cd /; run_site_hook {a}")
    cwd = Path(f"{harness['hook_calls']}.cwd").read_text().strip()
    assert cwd == str(harness["src"].resolve())


def test_a_sha_the_clone_cannot_check_out_alerts_once(harness: dict[str, Path]) -> None:
    clone_with(harness, 1)
    run(harness, f"run_site_hook {'e' * 40}")
    sent = alerts(harness)
    assert len(sent) == 1 and "(checkout)" in sent[0]
    assert hook_calls(harness) == []


def test_the_published_site_check_runs_the_hook(harness: dict[str, Path]) -> None:
    a, b = clone_with(harness, 2)
    harness["site_hooked"].write_text(a + "\n")
    harness["site_pending"].write_text(f"{b} 1\n")
    harness["body"].write_text(f'{{"sha": "{b}"}}')
    run(harness, "check_site")
    assert hook_calls(harness) == [f"{b}|{a}"]
    assert alerts(harness) == []


def test_no_url_means_no_call(harness: dict[str, Path]) -> None:
    run(harness, 'notify_failure "a" * 40', url="")
    assert calls(harness) == []


def test_failure_alerts_once_per_sha(harness: dict[str, Path], env: ProductSettings) -> None:
    run(harness, "notify_failure aaaaaaa1; notify_failure aaaaaaa1; notify_failure bbbbbbb2")
    sent = alerts(harness)
    assert len(sent) == 2
    assert "aaaaaaa" in sent[0] and "compose up" in sent[0] and "90s" in sent[0]
    assert f"{env.values['PRODUCT']} deploy FAILED" in sent[0], (
        "the message names the product from product.settings"
    )
    assert f"{env.values['COMPOSE_PROJECT']}-deployer-1" in sent[0]
    assert "bbbbbbb" in sent[1]
    assert harness["alerted"].read_text().strip() == "bbbbbbb2"


def test_url_travels_by_stdin_not_argv(harness: dict[str, Path]) -> None:
    run(harness, "notify_failure aaaaaaa1")
    lines = calls(harness)
    assert "bot123:abc" not in lines[0]
    assert "bot123:abc" in lines[1] and lines[1].startswith("stdin: url = ")
    assert "-K -" in lines[0] and "--data-urlencode" in lines[0]


def test_a_slow_telegram_is_retried_before_the_message_is_given_up_on(harness: dict[str, Path]) -> None:
    """curl treats a timeout as transient, so --retry turns one slow answer
    into a delivered message instead of a log line."""
    run(harness, "notify_failure ccccccc3")
    argv = next(line for line in calls(harness) if line.startswith("argv:"))
    assert "--retry 2" in argv and "--max-time 10" in argv


def test_a_deploy_alerts_only_when_it_ends_a_failure(harness: dict[str, Path]) -> None:
    out = run(harness, "notify_deployed ccccccc3")
    assert alerts(harness) == [] and "deployed ccccccc" in out
    run(harness, "notify_failure aaaaaaa1; notify_deployed ccccccc3; notify_deployed ddddddd4")
    sent = alerts(harness)
    assert len(sent) == 2
    assert "FAILED" in sent[0]
    assert "recovered" in sent[1] and "ccccccc" in sent[1]
    assert not harness["alerted"].exists()


def test_a_sha_that_changes_no_app_code_is_logged_not_alerted(harness: dict[str, Path]) -> None:
    out = run(harness, "notify_no_app_change eeeeeee5")
    assert alerts(harness) == []
    assert "changes nothing this server runs" in out and "eeeeeee" in out


def runs_response(*runs: tuple[str, str, str]) -> str:
    return json.dumps(
        {
            "total_count": len(runs),
            "workflow_runs": [
                {
                    "path": path,
                    "event": "push",
                    "status": "completed" if conclusion else "in_progress",
                    "conclusion": conclusion or None,
                    "html_url": url,
                }
                for path, conclusion, url in runs
            ],
        }
    )


def ci_head_then_runs(harness: dict[str, Path], sha: str, runs_json: str) -> None:
    # The fake curl replays one body for every call, so the sha lookup and the
    # runs lookup share it: a document that answers both questions at once.
    harness["body"].write_text(runs_json[:-1] + f', "sha": "{sha}"}}')


def test_a_failed_run_on_the_ci_branch_alerts_once_per_sha(harness: dict[str, Path]) -> None:
    sha = "f" * 40
    ci_head_then_runs(
        harness,
        sha,
        runs_response(
            (".github/workflows/tests.yml", "failure", "https://github.invalid/x/y/actions/runs/1"),
            (".github/workflows/promote.yml", "", "https://github.invalid/x/y/actions/runs/3"),
        ),
    )
    run(harness, "check_ci; check_ci")
    sent = alerts(harness)
    assert len(sent) == 1
    assert "pipeline FAILED" in sent[0] and "fffffff" in sent[0]
    assert "tests.yml failure https://github.invalid/x/y/actions/runs/1" in sent[0]
    assert "promote" not in sent[0]
    assert harness["ci_alerted"].read_text().strip() == sha


def test_a_green_or_unfinished_ci_branch_is_quiet(harness: dict[str, Path]) -> None:
    ci_head_then_runs(
        harness,
        "a" * 40,
        runs_response(
            (".github/workflows/tests.yml", "success", "https://github.invalid/x/y/actions/runs/1"),
            (".github/workflows/promote.yml", "", "https://github.invalid/x/y/actions/runs/2"),
        ),
    )
    run(harness, "check_ci")
    assert alerts(harness) == []
    assert not harness["ci_alerted"].exists()


def test_a_github_read_that_fails_alerts_nothing_and_is_not_fatal(harness: dict[str, Path]) -> None:
    harness["status"].write_text("22\n")
    out = run(harness, "check_ci; echo still-running")
    assert "still-running" in out
    assert alerts(harness) == []


def test_a_published_site_build_is_logged_not_alerted(harness: dict[str, Path]) -> None:
    sha = "b" * 40
    harness["body"].write_text(f'{{"sha": "{sha}"}}')
    out = run(harness, f"mark_site_pending {sha}; check_site; check_site", site_hook="")
    assert alerts(harness) == [] and "site published bbbbbbb" in out
    assert not harness["site_pending"].exists()


def test_a_site_build_missing_past_the_grace_period_alerts_once_then_recovers(
    harness: dict[str, Path],
) -> None:
    sha = "c" * 40
    harness["body"].write_text('{"sha": "0000000000000000000000000000000000000000"}')
    # Stamped long ago; checked twice, so the alert must not repeat.
    harness["site_pending"].write_text(f"{sha} 1\n")
    run(harness, "check_site; check_site", site_hook="")
    sent = alerts(harness)
    assert len(sent) == 1 and "has not published ccccccc" in sent[0] and "25 min" in sent[0]
    assert harness["site_alerted"].read_text().strip() == sha
    harness["body"].write_text(f'{{"sha": "{sha}"}}')
    run(harness, "check_site", site_hook="")
    sent = alerts(harness)
    assert len(sent) == 2 and "site build recovered" in sent[1]
    assert not harness["site_alerted"].exists() and not harness["site_pending"].exists()


def test_a_site_build_inside_the_grace_period_is_quiet(harness: dict[str, Path]) -> None:
    harness["body"].write_text('{"sha": "0000000000000000000000000000000000000000"}')
    run(harness, f"mark_site_pending {'d' * 40}; check_site")
    assert alerts(harness) == []
    assert harness["site_pending"].exists() and not harness["site_alerted"].exists()


def test_lost_alert_is_logged_but_not_fatal(harness: dict[str, Path]) -> None:
    harness["status"].write_text("22\n")
    out = run(harness, "notify_failure aaaaaaa1; echo still-running")
    assert "alert delivery failed" in out and "still-running" in out


@pytest.mark.parametrize(
    "bad", ['https://example.invalid/a\nurl = "https://evil.invalid/b"', 'https://example.invalid/a"x']
)
def test_a_url_that_could_break_the_curl_config_line_disables_alerts(
    harness: dict[str, Path], bad: str
) -> None:
    out = run(harness, "notify_failure aaaaaaa1; echo still-running", url=bad)
    assert calls(harness) == []
    assert "unexpected characters" in out and "still-running" in out


def startup_check() -> str:
    lines = LOOP.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith('if [ -n "$ALERT_URL" ]')]
    assert len(starts) == 1, "startup check moved; update this test"
    end = next(i for i in range(starts[0], len(lines)) if lines[i] == "fi")
    assert end - starts[0] <= 4, "the startup check grew; update this test"
    return "\n".join(lines[starts[0] : end + 1]) + "\n"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.telegram.org/bot123:AA-_x/sendMessage?chat_id=-100", "deploy alerts on"),
        ("https://example.invalid/a b", "unexpected characters; alerts off"),
    ],
)
def test_startup_reports_whether_alerts_will_work(harness: dict[str, Path], url: str, expected: str) -> None:
    out = run(harness, startup_check(), url=url)
    assert expected in out
    assert calls(harness) == []


def test_startup_is_silent_without_a_url(harness: dict[str, Path]) -> None:
    assert run(harness, startup_check(), url="") == ""


def test_the_loop_wires_every_alert_in() -> None:
    """The functions above are proved in isolation; this pins that the loop
    calls them, because a helper nobody calls passes every test of itself."""
    text = LOOP.read_text(encoding="utf-8")
    deploy_body = text[text.index("deploy() {") : text.index("app_changed() {")]
    assert 'notify_deployed "$sha"' in deploy_body and 'mark_site_pending "$sha"' in deploy_body
    # The release marker is written first and its failure returns before any
    # announcement: a deploy not recorded is retried, never celebrated.
    record = 'echo "$sha" 2>/dev/null >"$STATE" || return 1'
    assert record in deploy_body and deploy_body.index(record) < deploy_body.index('notify_deployed "$sha"')
    loop = text[text.index("while true; do") :]
    assert loop.index("check_ci") < loop.index("latest_sha") and loop.index("check_site") < loop.index(
        "latest_sha"
    )
    skipped = loop[loop.index("changes no app code") :]
    assert 'if echo "$sha" 2>/dev/null >"$STATE"; then' in skipped
    assert 'notify_no_app_change "$sha"' in skipped and 'mark_site_pending "$sha"' in skipped
    assert 'notify_failure "$sha"' in loop
    published = text[text.index('if [ "$live" = "$sha" ]; then') :]
    published = published[: published.index("now=$(date")]
    assert published.count('run_site_hook "$sha"') == 1, "the hook runs only once the site serves the sha"


def test_a_broken_pending_marker_cannot_end_the_loop(harness: dict[str, Path]) -> None:
    """read fails on an empty file and on a line with no newline; under set -e that
    would have ended the deployer for good, before any alert could say so."""
    harness["body"].write_text('{"sha": "0000000000000000000000000000000000000000"}')
    harness["site_pending"].write_text("")
    out = run(harness, "set -e; check_site; echo still-running")
    assert "still-running" in out and not harness["site_pending"].exists()
    harness["site_pending"].write_text("e" * 40)  # no stamp, no newline
    out = run(harness, "set -e; check_site; echo still-running")
    assert "still-running" in out
    assert alerts(harness) == []
    sha, stamped = harness["site_pending"].read_text().split()
    assert sha == "e" * 40 and stamped.isdigit()  # grace restarted, not alerted at epoch zero
    harness["site_pending"].write_text(f"{'f' * 40} 1 extra fields here\n")
    out = run(harness, "set -e; check_site; echo still-running")
    assert "still-running" in out
    assert len(alerts(harness)) == 1


def test_a_release_marker_that_is_not_a_sha_is_treated_as_unreadable(harness: dict[str, Path]) -> None:
    """The marker is public, and would be attacker-controlled if the site were;
    only a 40-hex value is compared, and anything else is never echoed."""
    harness["body"].write_text('{"sha": "junk; --output /tmp/x"}')
    harness["site_pending"].write_text(f"{'a' * 40} 1\n")  # long past the grace period
    run(harness, "check_site")
    sent = alerts(harness)
    assert len(sent) == 1 and "live: unreadable" in sent[0]
    assert "junk" not in sent[0] and "--output" not in sent[0]


def test_a_superseding_sha_drops_the_earlier_alert_state(harness: dict[str, Path]) -> None:
    harness["site_alerted"].write_text("c" * 40 + "\n")
    run(harness, f"mark_site_pending {'d' * 40}")
    assert not harness["site_alerted"].exists()


def test_the_deploy_message_carries_the_commit_subject(harness: dict[str, Path], tmp_path: Path) -> None:
    """subject_of once put -- before the sha, which git reads as a pathspec,
    so every message ended in a dangling dash; this builds a real repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = git_env()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "fix(x): the subject line"], check=True, env=env
    )
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, env=env).strip()
    run(harness, f'SRC="{repo}"; notify_failure {sha}; notify_deployed {sha}')
    sent = alerts(harness)[1:]
    assert len(sent) == 1 and "fix(x): the subject line" in sent[0]


def test_an_unwritable_state_directory_cannot_end_the_loop(harness: dict[str, Path], tmp_path: Path) -> None:
    """The alert-state writes and removals are the same shape as the marker
    write: a full or read-only state directory must cost a log line, never
    the deployer. The files exist before the directory is locked, so every
    rm -f has something it cannot unlink."""
    ro = tmp_path / "ro"
    ro.mkdir()
    old = "0" * 40
    (ro / "a").write_text(old + "\n")
    (ro / "s").write_text(old + "\n")
    (ro / "pending").write_text(f"{'c' * 40} 1\n")
    ro.chmod(0o555)
    try:
        sha = "f" * 40
        ci_head_then_runs(
            harness,
            sha,
            runs_response(
                (".github/workflows/tests.yml", "failure", "https://github.invalid/x/y/actions/runs/1")
            ),
        )
        out = run(
            harness,
            f'set -e; CI_ALERTED="{ro}/ci"; ALERTED="{ro}/a"; SITE_ALERTED="{ro}/s"; SITE_PENDING="{ro}/pending"; '
            f"check_ci; notify_failure {sha}; notify_deployed {sha}; check_site; mark_site_pending {sha}; "
            "echo still-running",
        )
    finally:
        ro.chmod(0o755)
    assert "still-running" in out and "Permission denied" not in out
    # Writing to a file that already exists succeeds in a read-only directory;
    # creating one (the CI marker) and unlinking (the two removals) do not.
    assert out.count("could not write") == 1 and out.count("could not remove") == 2
    sent = alerts(harness)
    # CI failure, deploy failure, the recovery, and the site build past its grace.
    assert len(sent) == 4 and "recovered" in sent[2] and "has not published" in sent[3]


def test_a_grace_period_that_is_not_a_number_falls_back_to_the_default(harness: dict[str, Path]) -> None:
    lines = [
        line.strip()
        for line in LOOP.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('[[ "$SITE_GRACE"')
    ]
    assert len(lines) == 1, "grace check moved; update this test"
    out = run(harness, f'SITE_GRACE=abc; {lines[0]}; echo "grace=$SITE_GRACE"')
    assert "not a number" in out and "grace=1500" in out
    out = run(harness, f'SITE_GRACE=600; {lines[0]}; echo "grace=$SITE_GRACE"')
    assert "not a number" not in out and "grace=600" in out


def site_startup() -> str:
    return block(LOOP, 'if [ -n "$SITE_URL" ]; then', "fi", ceiling=5)


def test_no_site_marker_is_one_startup_line_and_ignores_the_grace_period(harness: dict[str, Path]) -> None:
    out = run(harness, "SITE_GRACE=abc\n" + site_startup(), site_url="")
    assert "no site marker configured" in out and "not a number" not in out
    out = run(harness, "SITE_GRACE=600\n" + site_startup())
    assert "no site marker configured" not in out


def test_no_site_marker_means_no_pending_state_no_poll_and_no_alert(harness: dict[str, Path]) -> None:
    """After a deploy the loop marks the site pending and checks it every
    tick; with SITE_URL empty both are no-ops, so nothing is fetched and no
    site alert can ever go out."""
    sha = "c" * 40
    run(harness, f"mark_site_pending {sha}; check_site; check_site", site_url="")
    assert not harness["site_pending"].exists() and not harness["site_alerted"].exists()
    assert calls(harness) == []
    # A marker left from when SITE_URL was set, long past the grace period.
    harness["site_pending"].write_text(f"{sha} 1\n")
    run(harness, "check_site; check_site", site_url="")
    assert calls(harness) == [] and alerts(harness) == []
    assert not harness["site_alerted"].exists() and not harness["hook_calls"].exists()


def site_pair_check(site_url: str, hook: str) -> subprocess.CompletedProcess:
    code = (
        "set -euo pipefail\n"
        f"SITE_URL={shlex.quote(site_url)}; SITE_RELEASE_HOOK={shlex.quote(hook)}\n"
        + block(LOOP, 'SITE_URL="${SITE_URL:-}"', "fi", ceiling=5)
        + "echo STARTED\n"
    )
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("site_url", "hook"),
    [("", ""), ("https://site.invalid/_release.json", ""), ("https://site.invalid/_release.json", HOOK)],
)
def test_a_site_hook_with_its_marker_or_neither_starts_the_loop(site_url: str, hook: str) -> None:
    result = site_pair_check(site_url, hook)
    assert result.returncode == 0 and "STARTED" in result.stdout, result.stderr


def test_a_site_hook_without_a_site_marker_stops_the_loop() -> None:
    result = site_pair_check("", HOOK)
    assert result.returncode != 0 and "STARTED" not in result.stdout
    assert "SITE_RELEASE_HOOK" in result.stderr and "SITE_URL" in result.stderr


def test_a_stamp_with_leading_zeros_is_read_as_decimal(harness: dict[str, Path]) -> None:
    harness["body"].write_text('{"sha": "0000000000000000000000000000000000000000"}')
    harness["site_pending"].write_text(f"{'c' * 40} 0000000008\n")
    out = run(harness, "set -e; check_site; echo still-running")
    assert "still-running" in out
    sent = alerts(harness)
    assert len(sent) == 1 and "has not published" in sent[0]


def clone_copies(harness: dict[str, Path], env: ProductSettings) -> Path:
    return harness["src"] / env.values["LIBRARY_DIR"] / "deploy" / "deployer"


def bake(harness: dict[str, Path], env: ProductSettings, differing: tuple[str, ...] = ()) -> None:
    """The image's files and the clone's copies of them, the same but for `differing`."""
    clone = clone_copies(harness, env)
    for folder in (harness["baked"], clone):
        folder.mkdir(parents=True, exist_ok=True)
    for name in baked_names():
        (harness["baked"] / name).write_text(f"{name} as built\n")
        (clone / name).write_text(f"{name} as released\n" if name in differing else f"{name} as built\n")


def test_a_baked_file_the_release_changed_is_reported_once_with_its_name(
    harness: dict[str, Path], env: ProductSettings
) -> None:
    """The deployer never replaces itself: a release that changes its own
    script leaves the running image on the old one until someone does the
    rollout, with nothing saying so unless this does."""
    bake(harness, env, differing=("deploy-loop.sh",))
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert "still-running" in out and out.count("does not match the repository") == 1
    assert "(differing: deploy-loop.sh)" in out and "deployer rollout" in out
    sent = alerts(harness)
    assert len(sent) == 1 and "(differing: deploy-loop.sh)" in sent[0]
    # A new shell, as a restarted container is: the marker keeps it quiet.
    assert "does not match" not in run(harness, "check_self") and len(alerts(harness)) == 1
    # A different set of files is news.
    bake(harness, env, differing=("deploy-loop.sh", "ci-gate.sh"))
    run(harness, "check_self; check_self")
    sent = alerts(harness)
    assert len(sent) == 2 and "(differing: deploy-loop.sh ci-gate.sh)" in sent[1]
    # The rollout is reported once, and the marker goes.
    bake(harness, env)
    out = run(harness, "check_self; check_self")
    sent = alerts(harness)
    assert len(sent) == 3 and "matches the repository again" in sent[2]
    assert out.count("matches the repository again") == 1 and not harness["behind"].exists()


def test_an_image_that_matches_the_clone_says_nothing(harness: dict[str, Path], env: ProductSettings) -> None:
    bake(harness, env)
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert out.strip() == "still-running"
    assert calls(harness) == [] and not harness["behind"].exists()


def test_a_comparison_that_cannot_be_made_is_one_log_line_and_never_the_loop(
    harness: dict[str, Path], env: ProductSettings
) -> None:
    """No clone, a file that cannot be read, no cmp: none of them is a
    difference, a recovery, or a reason for the loop to end."""
    bake(harness, env)
    shutil.rmtree(harness["src"])
    harness["behind"].write_text("deploy-loop.sh\n")
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert "still-running" in out and out.count("could not compare") == 1
    assert all(name in out for name in baked_names())
    # Not a recovery either: the earlier report stands until a real comparison.
    assert alerts(harness) == [] and harness["behind"].read_text() == "deploy-loop.sh\n"
    harness["behind"].unlink()

    bake(harness, env)
    out = run(harness, "set -euo pipefail; cmp() { return 127; }; check_self; check_self; echo still-running")
    assert "still-running" in out and out.count("could not compare") == 1
    assert alerts(harness) == [] and not harness["behind"].exists()

    # A directory where the file should be: cmp cannot read it, as root or not.
    bake(harness, env, differing=("deploy-loop.sh",))
    unreadable = clone_copies(harness, env) / "ci-gate.jq"
    unreadable.unlink()
    unreadable.mkdir()
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert "still-running" in out and "Is a directory" not in out
    assert out.count("could not compare the deployer image with the repository: ci-gate.jq") == 1
    # The file that could be compared is still reported.
    sent = alerts(harness)
    assert len(sent) == 1 and "(differing: deploy-loop.sh)" in sent[0]


def test_a_symlinked_clone_copy_is_never_opened(harness: dict[str, Path], env: ProductSettings) -> None:
    """cmp follows a symlink, and one that points at a pipe would hold the loop
    for ever. A link is reported as not comparable, whatever it points at."""
    bake(harness, env)
    copy = clone_copies(harness, env) / "ci-gate.jq"
    target = copy.with_name("elsewhere")
    target.write_text("not the baked file\n")
    copy.unlink()
    copy.symlink_to(target)
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert "still-running" in out
    assert out.count("could not compare the deployer image with the repository: ci-gate.jq") == 1
    # Followed, the link would have read as a difference and raised an alert.
    assert alerts(harness) == [] and not harness["behind"].exists()


def test_a_marker_that_cannot_be_written_still_means_one_report(
    harness: dict[str, Path], env: ProductSettings
) -> None:
    """The marker only carries the report across a restart. A directory in its
    place can be neither written nor read, and the second tick of the same
    process must still be quiet."""
    harness["behind"].mkdir()
    bake(harness, env, differing=("ci-gate.sh",))
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert "still-running" in out and "Is a directory" not in out
    assert out.count("could not write") == 1 and out.count("does not match the repository") == 1
    assert len(alerts(harness)) == 1


def test_a_marker_that_cannot_be_removed_still_means_one_recovery(
    harness: dict[str, Path], env: ProductSettings
) -> None:
    harness["behind"].mkdir()
    bake(harness, env, differing=("ci-gate.sh",))
    clone = clone_copies(harness, env)
    out = run(
        harness,
        f'set -euo pipefail; check_self; cp "{harness["baked"]}/ci-gate.sh" "{clone}/ci-gate.sh"; '
        "check_self; check_self; echo still-running",
    )
    assert "still-running" in out and harness["behind"].is_dir()
    assert out.count("could not remove") == 1 and out.count("matches the repository again") == 1
    sent = alerts(harness)
    assert len(sent) == 2 and "does not match" in sent[0] and "matches the repository again" in sent[1]


def test_a_restarted_deployer_does_not_repeat_a_report_the_marker_holds(
    harness: dict[str, Path], env: ProductSettings
) -> None:
    harness["behind"].write_text("deploy-loop.sh ci-gate.jq\n")
    bake(harness, env, differing=("deploy-loop.sh", "ci-gate.jq"))
    out = run(harness, "set -euo pipefail; check_self; check_self; echo still-running")
    assert out.strip() == "still-running" and alerts(harness) == []


def test_the_baked_list_is_exactly_what_the_dockerfile_copies() -> None:
    """check_self compares "$SCRIPT_DIR/<name>" with the clone's
    deploy/deployer/<name>, so every COPY must keep the file's name and land
    beside the entrypoint. A file added to the image and not to BAKED would
    never be compared."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copied = []
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        *sources, dest = line.split()[1:]
        for source in sources:
            target = f"{dest}{source}" if dest.endswith("/") else dest
            assert target == f"/{source}", f"{line!r} renames or moves a baked file; update check_self"
            copied.append(source)
    assert copied and sorted(copied) == sorted(baked_names())
    assert 'ENTRYPOINT ["/bin/bash", "/deploy-loop.sh"]' in dockerfile
    assert all((LOOP.parent / name).is_file() for name in baked_names())


def test_the_loop_checks_its_own_image_once_the_tick_s_deploy_is_over() -> None:
    """Last in the tick, never inside deploy(): the check can report a stale
    image, and can never stand between a sha and its deploy. Its status is
    discarded, so a report that cannot be made never stops the loop."""
    text = LOOP.read_text(encoding="utf-8")
    assert "check_self" not in text[text.index("deploy() {") : text.index("app_changed() {")]
    loop = text[text.index("while true; do") :]
    assert loop.count("check_self") == 1
    assert (
        loop.index("could not read latest sha")
        < loop.index("\n    check_self || true\n")
        < loop.index('sleep "$POLL"')
    )


def test_a_symlinked_site_hook_is_refused_like_a_missing_one(harness: dict[str, Path]) -> None:
    """A symlink could point anywhere; followed, it would run whatever it names."""
    (a,) = clone_with(harness, 1, symlink=True)
    assert (harness["src"] / HOOK).is_symlink()
    run(harness, f"run_site_hook {a}")
    sent = alerts(harness)
    assert len(sent) == 1 and "is a symlink" in sent[0]
    assert hook_calls(harness) == [] and not harness["site_hooked"].exists()


def test_the_site_hook_never_sees_the_token_or_the_alert_url(harness: dict[str, Path]) -> None:
    """Both are exported in the deployer's container; the hook runs without them."""
    (a,) = clone_with(harness, 1)
    out = run(
        harness,
        f"export GITHUB_DEPLOY_TOKEN=fake-token DEPLOY_ALERT_URL=https://alerts.invalid/x; "
        f"printenv GITHUB_DEPLOY_TOKEN; run_site_hook {a}",
    )
    assert "fake-token" in out, "the control: the token is exported to children"
    assert hook_calls(harness) == [f"{a}|"]
    assert Path(f"{harness['hook_calls']}.vars").read_text() == "unset unset"


STATUS_TOKEN = "github_pat_FAKE0123"
STATUS_SHA = "a" * 40


def status_run(
    harness: dict[str, Path], token: str = STATUS_TOKEN, context: str = "deploy/live", more: str = ""
) -> str:
    settings = f"DEPLOY_STATUS_TOKEN={shlex.quote(token)}; DEPLOY_STATUS_CONTEXT={shlex.quote(context)}; "
    return run(harness, f"{settings}post_deploy_status {STATUS_SHA}{more}")


def status_calls(harness: dict[str, Path]) -> tuple[str, str]:
    """The one curl call as (argv, stdin); jq prints the body over several lines."""
    argv, stdin = "\n".join(calls(harness)).split("\nstdin: ")
    return argv, f"stdin: {stdin}"


def deploy_body() -> str:
    text = LOOP.read_text(encoding="utf-8")
    return text[text.index("deploy() {") : text.index("app_changed() {")]


def test_a_healthy_deploy_posts_one_success_status(harness: dict[str, Path]) -> None:
    status_run(harness)
    argv, _ = status_calls(harness)
    assert argv.count("argv:") == 1
    assert "POST" in argv and f"/statuses/{STATUS_SHA}" in argv
    body = json.loads(argv.split(" -d ", 1)[1].split(" https://", 1)[0])
    assert set(body) == {"state", "context", "description"}
    assert body["state"] == "success" and body["context"] == "deploy/live"
    assert re.fullmatch(r"Deployed and healthy at \d{4}-\d\d-\d\dT\d\d:\d\dZ", body["description"])


@pytest.mark.parametrize(
    "settings",
    [
        "",
        f"DEPLOY_STATUS_TOKEN={STATUS_TOKEN};",
        "DEPLOY_STATUS_CONTEXT=deploy/live;",
        f"DEPLOY_STATUS_TOKEN={STATUS_TOKEN}; DEPLOY_STATUS_CONTEXT=;",
        "DEPLOY_STATUS_TOKEN=; DEPLOY_STATUS_CONTEXT=deploy/live;",
    ],
)
def test_the_status_is_off_unless_both_settings_are_set(harness: dict[str, Path], settings: str) -> None:
    out = run(harness, f"set -u; {settings} post_deploy_status {STATUS_SHA}; echo still-running")
    assert "still-running" in out and calls(harness) == []


@pytest.mark.parametrize(
    ("token", "context"),
    [
        (STATUS_TOKEN, 'deploy"x'),
        (STATUS_TOKEN, "a b"),
        ("tok\nen", "deploy/live"),
        ('tok"en', "deploy/live"),
    ],
)
def test_a_bad_context_or_token_is_one_log_line_and_no_call(
    harness: dict[str, Path], token: str, context: str
) -> None:
    out = status_run(harness, token=token, context=context, more="; echo still-running")
    assert calls(harness) == [] and "still-running" in out
    assert out.count("unexpected characters; deploy status off") == 1


def test_a_failed_status_post_is_one_log_line_and_the_deploy_goes_on(harness: dict[str, Path]) -> None:
    harness["status"].write_text("22\n")
    out = run(
        harness,
        f"set -euo pipefail; DEPLOY_STATUS_TOKEN={STATUS_TOKEN}; DEPLOY_STATUS_CONTEXT=deploy/live; "
        f"post_deploy_status {STATUS_SHA}; mark_site_pending {STATUS_SHA}; echo still-running",
    )
    assert "still-running" in out and out.count("deploy status post failed") == 1
    assert harness["site_pending"].exists()
    assert STATUS_TOKEN not in out


def test_the_status_token_travels_by_stdin_and_is_never_logged(harness: dict[str, Path]) -> None:
    harness["body"].write_text(f"response {STATUS_TOKEN}")
    out = status_run(harness)
    argv, stdin = status_calls(harness)
    assert STATUS_TOKEN not in out and STATUS_TOKEN not in argv and "-K -" in argv
    assert stdin == f'stdin: header = "Authorization: Bearer {STATUS_TOKEN}"'
    assert "response" not in out


def test_the_status_is_posted_only_after_a_recorded_healthy_deploy() -> None:
    """A deploy that fails or is refused returns before the marker line, so
    the post, which sits after it, never runs; so is a rollback, which
    schema_compatible refuses before compose up."""
    body = deploy_body()
    call = body.index('post_deploy_status "$sha"')
    assert body.count('post_deploy_status "$sha"') == 1
    for earlier in (
        "schema_compatible || return 1",
        "up -d --no-build --remove-orphans || return 1",
        'FAILED_STEP="${API_SERVICE} readiness"',
        'FAILED_STEP="worker readiness"',
        'echo "$sha" 2>/dev/null >"$STATE" || return 1',
        'notify_deployed "$sha"',
    ):
        assert body.index(earlier) < call, earlier
    assert call < body.index('mark_site_pending "$sha"')
    assert LOOP.read_text(encoding="utf-8").count('post_deploy_status "$sha"') == 1, "no other path posts"


def test_the_hook_steps_run_without_the_status_token() -> None:
    lines = [
        line for line in LOOP.read_text(encoding="utf-8").splitlines() if "env -u GITHUB_DEPLOY_TOKEN" in line
    ]
    assert len(lines) == 3
    assert all("-u DEPLOY_ALERT_URL -u DEPLOY_STATUS_TOKEN" in line for line in lines)
