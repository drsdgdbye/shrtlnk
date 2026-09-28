#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
PROFILE="${SLOPO_PROFILE:-backend-go}"
SLOPO_SOURCE="${SLOPO_SOURCE:-$ROOT/../../slopo}"
export SLOPO_SOURCE
RATCHET="$ROOT/tools/slopo_harness/ratchet.py"
RUNNER=(uv run --offline --locked --project "$SLOPO_SOURCE" python "$RATCHET")
TOOLCHAIN="$ROOT/.slopo/$PROFILE-toolchain.json"
MAIN="$ROOT/.slopo/$PROFILE.yaml"
LOOSE="$ROOT/.slopo/$PROFILE-loose.yaml"
REGISTRY="$ROOT/.slopo/$PROFILE.accepted.json"
MANIFEST="$ROOT/.slopo/local/$PROFILE-current.json"
EVIDENCE="$ROOT/.slopo/local/$PROFILE-evidence.json"
PROPOSAL="$ROOT/.slopo/local/$PROFILE-reviewed-proposal.json"

case "${1:-}" in
  preflight)
    exec "${RUNNER[@]}" preflight --toolchain "$TOOLCHAIN" --main-config "$MAIN" --loose-config "$LOOSE"
    ;;
  snapshot)
    exec "${RUNNER[@]}" snapshot --toolchain "$TOOLCHAIN" --main-config "$MAIN" --loose-config "$LOOSE" --manifest "$MANIFEST" --evidence "$EVIDENCE"
    ;;
  gate)
    exec "${RUNNER[@]}" gate --toolchain "$TOOLCHAIN" --main-config "$MAIN" --loose-config "$LOOSE" --registry "$REGISTRY" --manifest "$MANIFEST" --evidence "$EVIDENCE"
    ;;
  propose)
    shift
    exec "${RUNNER[@]}" propose-baseline --repository-root "$ROOT" --manifest "$MANIFEST" --registry "$REGISTRY" --output "$PROPOSAL" "$@"
    ;;
  update)
    [[ -n "${2:-}" ]] || { echo 'usage: tools/slopo_harness.sh update sha256:<profile>' >&2; exit 2; }
    exec "${RUNNER[@]}" update-baseline --repository-root "$ROOT" --manifest "$MANIFEST" --proposal "$PROPOSAL" --registry "$REGISTRY" --confirm-profile "$2"
    ;;
  test)
    cd "$ROOT"
    exec uv run --offline --locked --project "$SLOPO_SOURCE" python -m unittest tools.slopo_harness.test_ratchet
    ;;
  *)
    echo 'usage: tools/slopo_harness.sh preflight|snapshot|gate|propose|update|test' >&2
    exit 2
    ;;
esac
