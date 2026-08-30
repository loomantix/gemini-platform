"""Execution contracts for the agent-loop Agy launchers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / ".agents/skills/agent-loop/scripts/run-agy-worker.sh"
REVIEW = ROOT / ".agents/skills/agent-loop/scripts/run-agy-review.sh"
CLAUDE_REVIEW = ROOT / ".agents/skills/agent-loop/scripts/run-claude-review.sh"
SHA = "a" * 40


def _subprocess_env() -> dict[str, str]:
    # These launchers intentionally execute standalone Python helpers and fake
    # CLIs. Pytest-cov exports COV_CORE_* for worker collection; inheriting it
    # makes those subprocesses emit statement-only data that cannot be combined
    # with this repository's branch coverage.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COV_CORE_")
    }


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_agy(tmp_path: Path, status: str = "SUCCESS") -> tuple[Path, Path]:
    argv_file = tmp_path / "agy-argv.json"
    agy = _executable(
        tmp_path / "agy",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as out:\n"
        "    json.dump(sys.argv[1:], out)\n"
        f"print(json.dumps({{'status': {status!r}, 'response': 'complete'}}))\n",
    )
    return agy, argv_file


def test_worker_uses_selected_model_and_requires_success_status(tmp_path: Path) -> None:
    agy, argv_file = _fake_agy(tmp_path)
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGY_ARGV_FILE": str(argv_file),
        "AGENT_LOOP_PROMPT": "Implement the bounded issue.",
    }
    result = subprocess.run(
        [str(WORKER), "--model", "gemini-primary"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "gemini-primary"
    assert "--disable-slash-commands" in argv

    failing_agy, _ = _fake_agy(tmp_path, status="ERROR")
    result = subprocess.run(
        [str(WORKER), "--model", "gemini-primary"],
        cwd=tmp_path,
        env={**env, "AGY_CLI": str(failing_agy)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "status 'ERROR'" in result.stderr


def _trusted_surface(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "trusted"
    surface = repo / ".agents"
    required = [
        "REVIEW_WORKFLOW.md",
        "references/local-review-ledger.md",
        "references/roles/code-reviewer.md",
        "references/roles/silent-failure-hunter.md",
        "references/roles/type-design-analyzer.md",
        "references/roles/comment-analyzer.md",
        "references/roles/pr-test-analyzer.md",
        "references/roles/security-reviewer.md",
        "skills/deepcritique/SKILL.md",
        "skills/critique/SKILL.md",
        "skills/critique/scripts/review-ledger.js",
        "skills/refactorpass/SKILL.md",
    ]
    for relative in required:
        path = surface / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trusted\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", ".agents"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "trusted surface"], check=True, capture_output=True)
    return repo, surface


def test_review_uses_truthful_engine_trusted_surface_and_fail_closed_json(
    tmp_path: Path,
) -> None:
    _, surface = _trusted_surface(tmp_path)
    issue = tmp_path / "issue"
    issue.mkdir()
    agy, argv_file = _fake_agy(tmp_path)
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGY_ARGV_FILE": str(argv_file),
        "AGENT_LOOP_REVIEW_ENGINE": "gemini",
        "AGENT_LOOP_REVIEW_BASE_SHA": SHA,
        "AGENT_LOOP_REVIEW_ROUND": "2",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": SHA,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "review-result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "review-push.sh"),
        "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
        "AGENT_LOOP_TRUSTED_BASE_REF": "HEAD",
    }
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-high"
    assert argv[argv.index("--print-timeout") + 1] == "1800s"
    assert argv[argv.index("--add-dir") + 1] == str(surface)
    assert "--disable-slash-commands" in argv
    prompt = argv[argv.index("--print") + 1]
    assert str(surface / "skills/deepcritique/SKILL.md") in prompt
    assert "engine gemini" in prompt
    # The round is handed over and deepcritique resolves the stance from it
    # (1-2 adversarial, 3+ convergence). Hardcoding convergence here cost three
    # of the six review lanes and the refactor pass on EVERY round, including
    # the first, which is the opposite of what the chain specifies.
    assert "round 2" in prompt
    assert "convergence mode" not in prompt

    result = subprocess.run(
        [str(REVIEW), "--engine", "claude"],
        cwd=issue,
        env={**env, "AGENT_LOOP_REVIEW_ENGINE": "claude"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    # Agy encodes effort in the Gemini model NAME and has no Claude effort
    # variants, so passing --effort with a Claude model is rejected outright:
    #   --effort is not supported for model "claude-sonnet-4-6"
    # The previous expectation here asserted the flag WAS passed, which is why
    # this suite stayed green while the lane could not start: the fake agy below
    # accepts any flags, so a mock can never surface a real model/flag
    # incompatibility. Assert the omission instead.
    assert "--effort" not in argv
    assert argv[argv.index("--print-timeout") + 1] == "1800s"
    assert "engine claude" in argv[argv.index("--print") + 1]

    failing_agy, _ = _fake_agy(tmp_path, status="CANCELED")
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env={**env, "AGY_CLI": str(failing_agy)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "status 'CANCELED'" in result.stderr


def test_review_honors_and_validates_custom_timeout(tmp_path: Path) -> None:
    _, surface = _trusted_surface(tmp_path)
    issue = tmp_path / "issue"
    issue.mkdir()
    agy, argv_file = _fake_agy(tmp_path)
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGY_ARGV_FILE": str(argv_file),
        "AGENT_LOOP_REVIEW_ENGINE": "gemini",
        "AGENT_LOOP_REVIEW_BASE_SHA": SHA,
        "AGENT_LOOP_REVIEW_ROUND": "1",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": SHA,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "push.sh"),
        "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
        "AGENT_LOOP_TRUSTED_BASE_REF": "HEAD",
        "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS": "900",
    }
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--print-timeout") + 1] == "900s"

    for invalid in ("0", "3601", "-10", "abc"):
        result = subprocess.run(
            [str(REVIEW), "--engine", "gemini"],
            cwd=issue,
            env={**env, "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS": invalid},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "LOCAL_REVIEW_PASS_TIMEOUT_SECONDS must be an integer from 1 through 3600" in result.stderr


def test_review_rejects_modified_trusted_surface_before_agy(tmp_path: Path) -> None:
    _, surface = _trusted_surface(tmp_path)
    (surface / "skills/deepcritique/SKILL.md").write_text("modified\n", encoding="utf-8")
    issue = tmp_path / "issue"
    issue.mkdir()
    marker = tmp_path / "agy-called"
    agy = _executable(tmp_path / "agy", f"#!/usr/bin/env bash\ntouch {marker}\n")
    env = {
        **_subprocess_env(),
        "AGY_CLI": str(agy),
        "AGENT_LOOP_REVIEW_ENGINE": "gemini",
        "AGENT_LOOP_REVIEW_BASE_SHA": SHA,
        "AGENT_LOOP_REVIEW_ROUND": "1",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": SHA,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "push.sh"),
        "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
        "AGENT_LOOP_TRUSTED_BASE_REF": "HEAD",
    }
    result = subprocess.run(
        [str(REVIEW), "--engine", "gemini"],
        cwd=issue,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "differs from the fetched base" in result.stderr
    assert not marker.exists()


def _fake_claude(tmp_path: Path, subtype: str = "success", is_error: bool = False) -> tuple[Path, Path]:
    argv_file = tmp_path / "claude-argv.json"
    cli = _executable(
        tmp_path / "claude",
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CLAUDE_ARGV_FILE'], 'w', encoding='utf-8') as out:\n"
        "    json.dump(sys.argv[1:], out)\n"
        f"print(json.dumps({{'type': 'result', 'subtype': {subtype!r},"
        f" 'is_error': {is_error!r}, 'result': 'complete'}}))\n",
    )
    return cli, argv_file


def _claude_argv(tmp_path: Path) -> list[str]:
    """The wrapper requires a non-builtin review hook to name both contract
    variables, so the launcher takes them as arguments and checks them against
    the environment."""
    return [
        str(CLAUDE_REVIEW),
        "--push-helper", str(tmp_path / "review-push.sh"),
        "--result-file", str(tmp_path / "review-result.json"),
    ]


def _claude_env(tmp_path: Path, surface: Path, cli: Path, argv_file: Path) -> dict[str, str]:
    return {
        **_subprocess_env(),
        "CLAUDE_REVIEW_CLI": str(cli),
        "CLAUDE_ARGV_FILE": str(argv_file),
        "AGENT_LOOP_REVIEW_ENGINE": "claude",
        "AGENT_LOOP_REVIEW_BASE_SHA": SHA,
        "AGENT_LOOP_REVIEW_ROUND": "1",
        "AGENT_LOOP_PR_NUMBER": "17",
        "AGENT_LOOP_PR_HEAD_SHA": SHA,
        "AGENT_LOOP_REVIEW_RESULT_FILE": str(tmp_path / "review-result.json"),
        "AGENT_LOOP_REVIEW_PUSH_HELPER": str(tmp_path / "review-push.sh"),
        "AGENT_LOOP_TRUSTED_AGENTS_ROOT": str(surface),
        "AGENT_LOOP_TRUSTED_BASE_REF": "HEAD",
    }


def test_claude_review_runs_the_claude_cli_on_the_trusted_surface(tmp_path: Path) -> None:
    """The Claude lane must run on the operator's own Claude entitlement.

    Agy exposes Claude models, but spending them draws on the Agy plan's
    allowance, which is provisioned for Gemini. This launcher exists so a
    consumer without an Anthropic allowance on that plan still gets a second,
    independent review engine.
    """
    _, surface = _trusted_surface(tmp_path)
    issue = tmp_path / "issue"
    issue.mkdir()
    cli, argv_file = _fake_claude(tmp_path)

    result = subprocess.run(
        _claude_argv(tmp_path),
        cwd=issue,
        env=_claude_env(tmp_path, surface, cli, argv_file),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--add-dir") + 1] == str(surface)
    assert argv[argv.index("--output-format") + 1] == "json"
    # No model is pinned, so the operator's configured default is used and this
    # launcher does not go stale when the model lineup moves.
    assert "--model" not in argv
    prompt = argv[argv.index("--print") + 1]
    assert str(surface / "skills/deepcritique/SKILL.md") in prompt
    assert "engine claude" in prompt
    assert "convergence mode" not in prompt


def test_claude_review_fails_closed_on_error_and_refusal(tmp_path: Path) -> None:
    """Exit 0 is not success. A refusal reports subtype != success, and an
    errored run can still exit 0 while setting is_error, so both are checked
    independently rather than trusting the exit code."""
    _, surface = _trusted_surface(tmp_path)
    issue = tmp_path / "issue"
    issue.mkdir()

    for subtype, is_error, expected in (
        ("error_max_turns", False, "subtype 'error_max_turns'"),
        ("success", True, "is_error True"),
    ):
        cli, argv_file = _fake_claude(tmp_path, subtype=subtype, is_error=is_error)
        result = subprocess.run(
            _claude_argv(tmp_path),
            cwd=issue,
            env=_claude_env(tmp_path, surface, cli, argv_file),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, f"{subtype}/{is_error} should fail closed"
        assert expected in result.stderr, result.stderr


def test_claude_review_requires_the_trusted_surface(tmp_path: Path) -> None:
    """The reviewer must never resolve its instructions from the issue worktree
    the worker just wrote. Shared with the Agy launcher via review-surface.sh."""
    _, surface = _trusted_surface(tmp_path)
    cli, argv_file = _fake_claude(tmp_path)
    env = _claude_env(tmp_path, surface, cli, argv_file)

    result = subprocess.run(
        _claude_argv(tmp_path),
        cwd=surface,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must be outside the issue worktree" in result.stderr
