"""Failure fixtures for the non-mutating agent-loop config doctor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / ".agents/skills/agent-loop/scripts/config-doctor.py"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "consumer"
    skill = project / ".agents/skills/agent-loop"
    scripts = skill / "scripts"
    ledger_dir = project / ".agents/skills/critique/scripts"
    scripts.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/agent-loop-state.py", scripts)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/review-push.sh", scripts)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/run-agy-launch.sh", scripts)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/run-agy-worker.sh", scripts)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/run-agy-review.sh", scripts)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/run-claude-review.sh", scripts)
    shutil.copy2(ROOT / ".agents/skills/agent-loop/scripts/review-surface.sh", scripts)
    shutil.copy2(ROOT / ".agents/skills/critique/scripts/review-ledger.js", ledger_dir)
    # See tests/test_agent_loop.py: sync ships the sibling ESM manifest, and a
    # CommonJS consumer root is the context that needs it.
    shutil.copy2(ROOT / ".agents/skills/critique/scripts/package.json", ledger_dir)
    (project / "package.json").write_text(
        '{"name": "fixture-consumer", "private": true, "type": "commonjs"}\n',
        encoding="utf-8",
    )
    (skill / "prompt.txt").write_text(
        "Read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY. Create a local commit only; do not push.\n",
        encoding="utf-8",
    )
    (project / "agent-loop-instructions.md").write_text(
        "The worker reads AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY.\n",
        encoding="utf-8",
    )
    (skill / "agent-loop.config").write_text(
        "review_contract_version = 3\n"
        'gemini_review_hook = "$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine gemini\n'
        'claude_review_hook = "$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine claude\n'
        "worker_hook =\n"
        "worker_model = gemini-primary\n"
        "worker_fallback_model = gemini-fallback\n",
        encoding="utf-8",
    )
    return project


def _run(project: Path, claude_effort: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(DOCTOR), "--project-dir", str(project),
         "--claude-effort", claude_effort],
        capture_output=True,
        text=True,
        check=False,
    )


CLAUDE_CLI_HOOK = (
    '"$AGENT_LOOP_TRUSTED_AGENTS_ROOT/skills/agent-loop/scripts/run-claude-review.sh"'
    ' --push-helper "$AGENT_LOOP_REVIEW_PUSH_HELPER"'
    ' --result-file "$AGENT_LOOP_REVIEW_RESULT_FILE"'
)


def _set_claude_hook(project: Path, hook: str) -> None:
    """Rewrite whatever claude_review_hook line is currently present, so a test
    can move the fixture between runtimes more than once."""
    config = project / ".agents/skills/agent-loop/agent-loop.config"
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("claude_review_hook = "):
            lines[index] = f"claude_review_hook = {hook}\n"
            replaced = True
            break
    assert replaced, "fixture has no claude_review_hook line"
    config.write_text("".join(lines), encoding="utf-8")


def test_doctor_accepts_current_contract_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = sorted(path.relative_to(project) for path in project.rglob("*"))
    result = _run(project)
    assert result.returncode == 0, result.stderr
    assert "compatible" in result.stdout
    assert sorted(path.relative_to(project) for path in project.rglob("*")) == before


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("review_contract_version = 3", "review_contract_version = 2", "must be 3"),
        ("--engine gemini", "--engine codex", "dedicated Agy launcher"),
        ("--engine claude", "--engine gemini", "dedicated Agy launcher"),
        ("gemini_review_hook", "codex_review_hook", "dedicated Agy launcher"),
        ("worker_model = gemini-primary", "worker_model =", "worker_model is required"),
    ],
)
def test_doctor_failure_fixtures(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    project = _project(tmp_path)
    config = project / ".agents/skills/agent-loop/agent-loop.config"
    config.write_text(config.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    result = _run(project)
    assert result.returncode != 0
    assert message in result.stderr


def test_doctor_rejects_worker_prompt_that_requires_masked_gh(tmp_path: Path) -> None:
    project = _project(tmp_path)
    prompt = project / ".agents/skills/agent-loop/prompt.txt"
    prompt.write_text(
        "Run gh issue view 7, then read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY. Create a local commit; do not push.\n",
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode != 0
    assert "require masked gh" in result.stderr


def test_doctor_rejects_worker_instructions_that_require_masked_gh(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    instructions = project / "agent-loop-instructions.md"
    instructions.write_text(
        "Read AGENT_LOOP_ISSUE_TITLE and AGENT_LOOP_ISSUE_BODY, then run gh pr create.\n",
        encoding="utf-8",
    )
    result = _run(project)
    assert result.returncode != 0
    assert "require masked gh" in result.stderr


def test_doctor_rejects_incompatible_review_push_protocol(tmp_path: Path) -> None:
    project = _project(tmp_path)
    review_push = project / ".agents/skills/agent-loop/scripts/review-push.sh"
    review_push.write_text("#!/usr/bin/env bash\nprintf '2\\n'\n", encoding="utf-8")
    review_push.chmod(0o755)
    result = _run(project)
    assert result.returncode != 0
    assert "review-push protocol is incompatible" in result.stderr


def test_doctor_rejects_a_claude_effort_policy_the_agy_lane_cannot_honour(
    tmp_path: Path,
) -> None:
    """Agy encodes effort in the Gemini model NAME and ships no Claude effort
    variants, so `--effort` with a Claude model is rejected by the CLI:

        invalid model selection (--model "claude-sonnet-4-6" --effort "low"):
        --effort is not supported for model "claude-sonnet-4-6"

    That killed a live agent-loop run at the Claude lane, on round 1, after the
    worker and both validations had already passed. The policy is unsatisfiable
    on this path, so it must fail at preflight rather than mid-review."""
    project = _project(tmp_path)
    result = _run(project, claude_effort="low")
    assert result.returncode != 0
    assert "cannot be honoured by the Agy review launcher" in result.stderr


def test_doctor_accepts_the_claude_cli_lane_and_enforces_its_effort_policy(
    tmp_path: Path,
) -> None:
    """The Claude lane may run on the operator's own entitlement instead of the
    Agy plan's allowance. On that path a claude_effort_policy IS honourable, so
    the hook must pass it — an unenforced policy is not a policy."""
    project = _project(tmp_path)
    _set_claude_hook(project, CLAUDE_CLI_HOOK + ' --effort low')
    assert _run(project, claude_effort="low").returncode == 0

    # Same hook, policy declared but not passed through.
    _set_claude_hook(project, CLAUDE_CLI_HOOK)
    result = _run(project, claude_effort="low")
    assert result.returncode != 0
    assert "must pass --effort low" in result.stderr


def test_doctor_requires_the_claude_hook_to_carry_the_contract_paths(
    tmp_path: Path,
) -> None:
    """agent-loop.sh rejects a non-builtin hook that does not name both contract
    variables. Catch it here, where the message can say which one is missing."""
    project = _project(tmp_path)
    _set_claude_hook(
        project,
        '"$AGENT_LOOP_TRUSTED_AGENTS_ROOT/skills/agent-loop/scripts/run-claude-review.sh"'
        ' --push-helper "$AGENT_LOOP_REVIEW_PUSH_HELPER"',
    )
    result = _run(project)
    assert result.returncode != 0
    assert "must pass $AGENT_LOOP_REVIEW_RESULT_FILE" in result.stderr


def test_doctor_rejects_an_unknown_claude_runtime(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _set_claude_hook(project, '"$SOME_OTHER_LAUNCHER" --engine claude')
    result = _run(project)
    assert result.returncode != 0
    assert "claude_review_hook must use the dedicated Agy launcher or" in result.stderr
