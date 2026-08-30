#!/usr/bin/env python3
"""Non-mutating compatibility preflight for consumer agent-loop configuration."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


class DoctorError(RuntimeError):
    """An incompatible consumer configuration."""


def _config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z_]+)\s*=\s*(.*)", line)
        if match is None:
            raise DoctorError(f"invalid config line: {raw}")
        key, value = match.groups()
        if key in values:
            raise DoctorError(f"duplicate config key: {key}")
        values[key] = value.rstrip()
    return values


def _version(command: list[str], label: str) -> str:
    # `command[0]` may be a PATH lookup (`node`) rather than an interpreter we
    # know exists, so a missing runtime must read as a doctor failure instead
    # of an uncaught OSError traceback.
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise DoctorError(f"{label} could not be executed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "<no stderr>"
        raise DoctorError(
            f"{label} compatibility query failed (exit {result.returncode}): {detail}"
        )
    return result.stdout.strip()


def doctor(project: Path, claude_effort: str | None) -> None:
    root = project.resolve()
    skill = root / ".agents/skills/agent-loop"
    config_path = skill / "agent-loop.config"
    prompt_path = skill / "prompt.txt"
    instructions_path = root / "agent-loop-instructions.md"
    ledger = root / ".agents/skills/critique/scripts/review-ledger.js"
    state = skill / "scripts/agent-loop-state.py"
    review_push = skill / "scripts/review-push.sh"
    worker_launcher = skill / "scripts/run-agy-worker.sh"
    review_launcher = skill / "scripts/run-agy-review.sh"
    launch_helper = skill / "scripts/run-agy-launch.sh"
    for path in (
        config_path,
        prompt_path,
        instructions_path,
        ledger,
        state,
        review_push,
        worker_launcher,
        review_launcher,
        launch_helper,
    ):
        if not path.is_file() or path.is_symlink():
            raise DoctorError(f"required agent-loop file is missing: {path.relative_to(root)}")
    values = _config(config_path)
    if values.get("review_contract_version") != "3":
        raise DoctorError("review_contract_version must be 3")
    if _version(["node", str(ledger), "--protocol-version"], "review ledger") != "3":
        raise DoctorError("review-ledger protocol is incompatible with contract v3")
    if _version([sys.executable, str(state), "--state-version"], "run state") != "2":
        raise DoctorError("agent-loop state protocol is incompatible")
    if _version([str(review_push), "--protocol-version"], "review push") != "1":
        raise DoctorError("review-push protocol is incompatible")

    prompt = prompt_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8")
    for token in ("AGENT_LOOP_ISSUE_TITLE", "AGENT_LOOP_ISSUE_BODY"):
        if token not in prompt:
            raise DoctorError(f"worker prompt must read {token}")
    if re.search(r"\bgh\s+(?:api|issue|pr|repo)\b", prompt + "\n" + instructions):
        raise DoctorError("worker prompt or instructions require masked gh")
    if "local commit" not in prompt.lower() or "do not push" not in prompt.lower():
        raise DoctorError("worker prompt must require a local commit and forbid push")
    if "AGENT_LOOP_ISSUE_TITLE" not in instructions or "AGENT_LOOP_ISSUE_BODY" not in instructions:
        raise DoctorError("worker instructions must describe wrapper-provided issue context")

    hooks = {
        "gemini": values.get("gemini_review_hook", ""),
        "claude": values.get("claude_review_hook", ""),
    }
    if hooks["gemini"] != '"$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine gemini':
        raise DoctorError("gemini_review_hook must use the dedicated Agy launcher")

    # The Claude lane has two legitimate runtimes, and the choice is an
    # ENTITLEMENT question, not a preference. Agy exposes Claude models, but
    # spending them draws on the Agy plan's allowance, which is provisioned for
    # Gemini; a consumer whose plan carries no Anthropic allowance cannot use
    # that path at all and must run the Claude CLI on its own entitlement.
    claude_agy_hook = '"$AGENT_LOOP_AGY_REVIEW_LAUNCHER" --engine claude'
    claude_cli_launcher = (
        '"$AGENT_LOOP_TRUSTED_AGENTS_ROOT/skills/agent-loop/scripts/run-claude-review.sh"'
    )
    claude_hook = hooks["claude"]
    claude_uses_agy = claude_hook == claude_agy_hook
    claude_uses_cli = claude_hook.startswith(claude_cli_launcher)
    if not claude_uses_agy and not claude_uses_cli:
        raise DoctorError(
            "claude_review_hook must use the dedicated Agy launcher or "
            "run-claude-review.sh from the trusted agents root"
        )
    if claude_uses_cli:
        # agent-loop.sh requires a non-builtin hook to name both contract
        # variables, and run-claude-review.sh verifies the values it is handed
        # against the environment. Check the hook string carries them so the
        # failure lands here, at preflight, rather than mid-review.
        for variable in (
            "AGENT_LOOP_REVIEW_PUSH_HELPER",
            "AGENT_LOOP_REVIEW_RESULT_FILE",
        ):
            if variable not in claude_hook:
                raise DoctorError(f"claude_review_hook must pass ${variable}")
    if values.get("worker_hook", ""):
        if values.get("worker_fallback_model", ""):
            raise DoctorError("worker_fallback_model cannot be used with a custom worker_hook")
    elif not values.get("worker_model", ""):
        raise DoctorError("worker_model is required for the default Agy worker")
    # Agy encodes reasoning effort in the Gemini MODEL NAME and publishes no
    # Claude effort variants, so `--effort` with a Claude model is rejected by
    # the CLI outright. A claude_effort_policy is therefore unsatisfiable on the
    # Agy path and must fail here rather than crashing the lane mid-run:
    #   invalid model selection (--model "claude-sonnet-4-6" --effort "low"):
    #   --effort is not supported for model "claude-sonnet-4-6"
    if claude_effort and claude_uses_agy:
        raise DoctorError(
            "claude_effort_policy cannot be honoured by the Agy review launcher: "
            "Agy supports no Claude effort variants. Leave it empty, or run the "
            "Claude lane through run-claude-review.sh, which takes --effort."
        )
    # On the CLI path the policy IS honourable, so require the hook to pass it
    # literally — an unenforced policy is not a policy.
    if claude_effort and claude_uses_cli:
        if f'--effort {claude_effort}' not in claude_hook:
            raise DoctorError(
                f"claude_review_hook must pass --effort {claude_effort} "
                "to match claude_effort_policy"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--claude-effort")
    args = parser.parse_args()
    doctor(Path(args.project_dir), args.claude_effort)
    print("agent-loop config doctor: compatible")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DoctorError, OSError) as error:
        print(f"agent-loop config doctor: {error}", file=sys.stderr)
        raise SystemExit(1) from error
