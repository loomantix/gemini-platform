#!/usr/bin/env bash
# Trusted-surface, fail-closed launcher for the Agy review engines.
set -euo pipefail

usage() {
    echo "usage: $0 --engine gemini|claude" >&2
    exit 2
}

engine=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --engine) [ "$#" -ge 2 ] || usage; engine="$2"; shift 2 ;;
        *) usage ;;
    esac
done

# Agy encodes reasoning effort in the Gemini MODEL NAME and exposes no effort
# variants for Claude, so `--effort` is Gemini-only. Passing it with a Claude
# model is not merely redundant, it is rejected outright:
#   invalid model selection (--model "claude-sonnet-4-6" --effort "low"):
#   --effort is not supported for model "claude-sonnet-4-6"
# An empty effort here means the flag is omitted below.
case "$engine" in
    gemini) model="gemini-3.7-flash-high"; effort="high" ;;
    claude) model="claude-sonnet-4-6"; effort="" ;;
    *) usage ;;
esac

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=review-surface.sh
source "$SCRIPT_DIR/review-surface.sh"

# shellcheck source=run-agy-launch.sh
source "$SCRIPT_DIR/run-agy-launch.sh"

effort_args=()
if [ -n "$effort" ]; then
    effort_args=(--effort "$effort")
fi

run_agy_and_parse "agy review" \
    --model "$model" \
    "${effort_args[@]}" \
    --mode accept-edits \
    --dangerously-skip-permissions \
    --disable-slash-commands \
    --add-dir "$trusted_root" \
    --output-format json \
    --print-timeout "${review_timeout_seconds}s" \
    --print "$prompt"
