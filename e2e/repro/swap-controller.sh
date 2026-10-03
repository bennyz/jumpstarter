#!/usr/bin/env bash
# Rebuilds the controller image from a jumpstarter checkout and swaps it into the
# e2e Kind cluster, so report-status-fencing.sh can run against a pre-fencing
# controller and back. The Python side is untouched: jmp keeps sending
# lease_name, which a pre-fencing controller ignores.
#
# Usage:
#   e2e/repro/swap-controller.sh ../jumpstarter-main   # pre-fencing controller
#   e2e/repro/report-status-fencing.sh --expect stale
#   e2e/repro/swap-controller.sh .                     # back to this tree
#
# Only the controller deployment restarts; the router keeps its current image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ $# -ne 1 ]; then
    sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit 2
fi
SRC="$(cd "$1" && pwd)"
NS="${E2E_TEST_NS:-jumpstarter-lab}"
CLUSTER="${KIND_CLUSTER_NAME:-jumpstarter}"
IMG="${IMG:-quay.io/jumpstarter-dev/jumpstarter-controller:latest}"
KIND="${KIND:-$REPO_ROOT/controller/bin/kind}"
if [ ! -x "$KIND" ]; then
    KIND="$(command -v kind)"
fi

echo "Building $IMG from $SRC ($(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown))"
make -C "$SRC/controller" docker-build IMG="$IMG"

if ! "$KIND" load docker-image "$IMG" --name "$CLUSTER" 2>/dev/null; then
    podman save "$IMG" | "$KIND" load image-archive /dev/stdin --name "$CLUSTER"
fi

# Same tag with IfNotPresent: replacing the pods picks up the freshly loaded image.
# shellcheck disable=SC2016 # go-template, not shell
selector="$(kubectl -n "$NS" get deployment jumpstarter-controller \
    -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}')"
selector="${selector%,}"
kubectl -n "$NS" delete pod -l "$selector" --wait=true
kubectl -n "$NS" rollout status deployment/jumpstarter-controller --timeout=180s
kubectl -n "$NS" wait --for=condition=Ready pod -l "$selector" --timeout=180s
kubectl -n "$NS" get pods -l "$selector" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[0].imageID}{"\n"}{end}'
