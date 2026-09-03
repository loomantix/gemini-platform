#!/usr/bin/env bash
# Trusted-surface, fail-closed launcher for the agent-loop Claude review engine.
#
# WHY THIS EXISTS SEPARATELY FROM run-agy-review.sh. Agy exposes Claude models,
# so the Agy launcher can nominally run the `claude` engine — but that spends the
# Agy plan's model allowance, which is provisioned for Gemini. A consumer whose
# Agy plan carries no Anthropic allowance cannot use that path at all. This
# launcher runs the Claude CLI directly instead, on the operator's own Claude
# entitlement, and is what `claude_review_hook` should point at in that case.
#
# It shares review-surface.sh with the Agy launcher, so both enforce exactly one
# definition of the trusted-surface contract and the review prompt.
set -euo pipefail

usage() {
    echo "usage: $0 --push-helper PATH --result-file PATH [--model MODEL] [--effort low|medium|high]" >&2
    exit 2
}

# The wrapper matches this against AGENT_LOOP_REVIEW_ENGINE in review-surface.sh.
engine="claude"
model="${AGENT_LOOP_CLAUDE_REVIEW_MODEL:-}"
# Unlike Agy, the Claude CLI takes effort as its own flag rather than encoding it
# in the model name, so a consumer's claude_effort_policy is honourable here.
effort=""
push_helper=""
result_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --model) [ "$#" -ge 2 ] || usage; model="$2"; shift 2 ;;
        --effort) [ "$#" -ge 2 ] || usage; effort="$2"; shift 2 ;;
        --push-helper) [ "$#" -ge 2 ] || usage; push_helper="$2"; shift 2 ;;
        --result-file) [ "$#" -ge 2 ] || usage; result_file="$2"; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$push_helper" ] && [ -n "$result_file" ] || usage

# These are passed as arguments AND read from the environment, so the two must
# agree. agent-loop.sh requires a non-builtin review hook to name both variables
# in its command string; taking them as arguments satisfies that honestly rather
# than mentioning them decoratively, and a mismatch means the hook string and the
# wrapper disagree about where the result goes — which must never be papered over.
[ "$push_helper" = "${AGENT_LOOP_REVIEW_PUSH_HELPER:-}" ] || {
    echo "--push-helper does not match AGENT_LOOP_REVIEW_PUSH_HELPER" >&2
    exit 1
}
[ "$result_file" = "${AGENT_LOOP_REVIEW_RESULT_FILE:-}" ] || {
    echo "--result-file does not match AGENT_LOOP_REVIEW_RESULT_FILE" >&2
    exit 1
}

claude_cli="${CLAUDE_REVIEW_CLI:-claude}"
command -v "$claude_cli" >/dev/null 2>&1 || { echo "claude CLI is required" >&2; exit 1; }
command -v timeout >/dev/null 2>&1 || { echo "timeout is required" >&2; exit 1; }

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=review-surface.sh
source "$SCRIPT_DIR/review-surface.sh"

# The CLI has no inner print deadline of its own, so the bound is external.
# `timeout` sends TERM at the deadline; the outer wrapper's hook_timeout_seconds
# must stay above this so this bound fires first and the failure is attributable.
cli_args=()
if [ -n "$model" ]; then
    cli_args+=(--model "$model")
fi
if [ -n "$effort" ]; then
    cli_args+=(--effort "$effort")
fi

RESULT_FILE="$(mktemp)"
trap 'rm -f -- "${RESULT_FILE:-}"' EXIT
chmod 600 "$RESULT_FILE"

cli_exit=0
timeout "${review_timeout_seconds}s" "$claude_cli" \
    "${cli_args[@]}" \
    --permission-mode bypassPermissions \
    --add-dir "$trusted_root" \
    --output-format json \
    --print "$prompt" >"$RESULT_FILE" || cli_exit="$?"

CLAUDE_REVIEW_EXIT="$cli_exit" python3 - "$RESULT_FILE" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
exit_code = int(os.environ["CLAUDE_REVIEW_EXIT"])
if exit_code == 124:
    raise SystemExit("claude review exceeded its deadline (timeout 124)")
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit(f"claude review returned invalid JSON (exit {exit_code}): {error}")

# Fail closed on every field independently: a run can exit 0 while reporting
# is_error, and a refusal still carries subtype != "success".
kind = payload.get("type")
subtype = payload.get("subtype")
is_error = payload.get("is_error")
response = payload.get("result")
if exit_code != 0 or kind != "result" or subtype != "success" or is_error is not False:
    detail = response or payload.get("error") or "no error detail"
    raise SystemExit(
        f"claude review failed (exit {exit_code}, type {kind!r}, "
        f"subtype {subtype!r}, is_error {is_error!r}): {detail}"
    )
if not isinstance(response, str) or not response.strip():
    raise SystemExit("claude review succeeded without a text response")
print(response)
PY
