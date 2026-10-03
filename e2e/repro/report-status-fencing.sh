#!/usr/bin/env bash
# Reproduces a late exporter ReportStatus overwriting newer exporter state on the
# local e2e Kind cluster, driven by ordinary jmp users.
#
# The exporter reaches the controller through reportstatus-holdproxy
# (controller/test/repro/reportstatus-holdproxy). The proxy delivers one chosen
# ReportStatus after the exporter's 30s deadline, on a context the exporter can
# no longer cancel, so the controller processes lease A's report after the
# exporter has retried, moved on and reported for the next lease. Everything
# else is unmodified jmp: alice, bob and charlie create leases, run
# `j power on`, and release them.
#
# Scenarios:
#   stale-available    A's Available lands after B is assigned and LeaseReady.
#                      Unfenced: the exporter reads Available while leased to B,
#                      and bob's `jmp shell` is refused as not ready.
#   stale-lease-ready  A's LeaseReady lands after A ended and the exporter went
#                      Available. Unfenced: the idle exporter reads LeaseReady
#                      and charlie's lease stays Pending.
# Every run also asserts that legitimate reports are accepted: unleased startup
# and shutdown, the retried cleanup report that lands after LeaseRef cleared,
# and normal cleanup at lease end.
#
# Usage:
#   e2e/repro/report-status-fencing.sh [--scenario all|stale-available|stale-lease-ready]
#                                      [--legacy-exporter] [--expect fenced|stale] [--keep]
#
#   --legacy-exporter  strip lease_name from every report, as a pre-fencing exporter sends
#   --expect           outcome to assert for the late reports. Defaults to fenced, or stale
#                      with --legacy-exporter. Use stale against a pre-fencing controller
#                      (see swap-controller.sh).
#   --keep             leave the exporter, proxy, leases and CRs in place on exit
#
# Environment: JMP (jmp binary), PROXY_ADDR, ADMIN_ADDR, HOLDPROXY_UPSTREAM_INSECURE=1.
# Requires `make e2e-setup`, go and curl. A full run takes about 3 minutes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=../lib/common.sh
source "$REPO_ROOT/e2e/lib/common.sh"

usage() {
    sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

SCENARIO=all
LEGACY=0
EXPECT=""
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --scenario) SCENARIO="$2"; shift 2 ;;
        --legacy-exporter) LEGACY=1; shift ;;
        --expect) EXPECT="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log_error "unknown argument: $1"; usage; exit 2 ;;
    esac
done
case "$SCENARIO" in
    all|stale-available|stale-lease-ready) ;;
    *) log_error "unknown scenario: $SCENARIO"; exit 2 ;;
esac
if [ -z "$EXPECT" ]; then
    if [ "$LEGACY" = 1 ]; then EXPECT=stale; else EXPECT=fenced; fi
fi
case "$EXPECT" in
    fenced|stale) ;;
    *) log_error "--expect must be fenced or stale"; exit 2 ;;
esac

if ! load_setup_config "$REPO_ROOT"; then
    log_error "e2e environment not found; run 'make e2e-setup' first"
    exit 1
fi
export SSL_CERT_FILE REQUESTS_CA_BUNDLE
NS="$E2E_TEST_NS"
if [ -z "$ENDPOINT" ]; then
    log_error "ENDPOINT missing from .e2e-setup-complete"
    exit 1
fi

JMP="${JMP:-$REPO_ROOT/python/.venv/bin/jmp}"
if [ ! -x "$JMP" ]; then
    JMP="$(command -v jmp || true)"
fi
if [ -z "$JMP" ]; then
    log_error "jmp not found; run 'make e2e-setup' or set JMP"
    exit 1
fi
# `j` inside `jmp shell` must come from the same installation.
PATH="$(dirname "$JMP"):$PATH"

for tool in kubectl curl go yq base64; do
    if ! command -v "$tool" >/dev/null; then
        log_error "$tool not found on PATH"
        exit 1
    fi
done

PROXY_ADDR="${PROXY_ADDR:-127.0.0.1:18082}"
ADMIN_ADDR="${ADMIN_ADDR:-127.0.0.1:18090}"
RUN="$(date +%H%M%S)"
EXPORTER="rsf-$RUN-exporter"
SELECTOR="rsf-run=$RUN"
WORK="$REPO_ROOT/.e2e/repro/report-status-fencing-$RUN"
START_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LEASES=()
RESULTS=()
FAILED=0
PROXY_PID=""
PROXY_TLS=()
mkdir -p "$WORK"

step() { echo; echo -e "${YELLOW}==> $*${NC}"; }
say() { local who=$1; shift; echo -e "${GREEN}[$who]${NC} $*"; }
pass() { RESULTS+=("PASS  $*"); log_info "PASS: $*"; }
fail() { RESULTS+=("FAIL  $*"); log_error "FAIL: $*"; FAILED=1; }

verdict() {
    local name=$1 observed=$2
    if [ "$observed" = "$EXPECT" ]; then
        pass "$name: $observed (expected $EXPECT)"
    else
        fail "$name: $observed (expected $EXPECT)"
    fi
}

# jmp_as WHO ARGS... runs jmp with WHO's client config, inserted before `--`.
jmp_as() {
    local who=$1; shift
    local args=() arg inserted=0
    for arg in "$@"; do
        if [ "$arg" = "--" ] && [ "$inserted" = 0 ]; then
            args+=(--client-config "$WORK/$who.yaml")
            inserted=1
        fi
        args+=("$arg")
    done
    if [ "$inserted" = 0 ]; then
        args+=(--client-config "$WORK/$who.yaml")
    fi
    echo -e "${GREEN}[$who]${NC} \$ jmp $*"
    "$JMP" "${args[@]}"
}

admin() {
    curl -sS --fail-with-body -X "$1" "http://$ADMIN_ADDR$2"
}

proxy_log_has() {
    admin GET /log | grep -Eq "$1"
}

hold_id() {
    local id=${1#id=}
    echo "${id%% *}"
}

exporter_state() {
    kubectl -n "$NS" get "exporters.jumpstarter.dev/$EXPORTER" \
        -o jsonpath='{.status.exporterStatus}|{.status.leaseRef.name}' 2>/dev/null
}

# wait_state WANT [TIMEOUT] waits for "<exporterStatus>|<leaseRef>".
wait_state() {
    local want=$1 timeout=${2:-120} state=""
    local deadline=$((SECONDS + timeout))
    while [ "$SECONDS" -lt "$deadline" ]; do
        state="$(exporter_state || true)"
        if [ "$state" = "$want" ]; then
            return 0
        fi
        sleep 1
    done
    log_warn "exporter is '$state', expected '$want' (status|leaseRef)"
    return 1
}

wait_assigned() {
    local lease=$1 timeout=${2:-30} ref=""
    local deadline=$((SECONDS + timeout))
    while [ "$SECONDS" -lt "$deadline" ]; do
        ref="$(kubectl -n "$NS" get "leases.jumpstarter.dev/$lease" \
            -o jsonpath='{.status.exporterRef.name}' 2>/dev/null || true)"
        if [ "$ref" = "$EXPORTER" ]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

show_lease() {
    kubectl -n "$NS" get "leases.jumpstarter.dev/$1" \
        -o jsonpath='{range .status.conditions[*]}    {.type}={.status} {.reason}: {.message}{"\n"}{end}' || true
}

start_proxy() {
    local extra=() i
    if [ "$LEGACY" = 1 ]; then
        extra+=(-strip-lease-name)
    fi
    step "Starting reportstatus-holdproxy: exporter -> $PROXY_ADDR -> $ENDPOINT"
    go build -C "$REPO_ROOT/controller" -o "$WORK/holdproxy" ./test/repro/reportstatus-holdproxy
    "$WORK/holdproxy" -listen "$PROXY_ADDR" -admin "$ADMIN_ADDR" -upstream "$ENDPOINT" \
        "${PROXY_TLS[@]}" ${extra[@]+"${extra[@]}"} -ca-out "$WORK/proxy-ca.pem" \
        >"$WORK/holdproxy.log" 2>&1 &
    PROXY_PID=$!
    for i in $(seq 1 50); do
        if admin GET /healthz >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "$PROXY_PID" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
    log_error "holdproxy did not start:"
    cat "$WORK/holdproxy.log"
    return 1
}

create_identities() {
    local who
    step "Creating exporter $EXPORTER ($SELECTOR) and clients alice, bob, charlie"
    for who in alice bob charlie; do
        "$JMP" admin create client -n "$NS" "rsf-$RUN-$who" \
            --unsafe --nointeractive --out "$WORK/$who.yaml" >/dev/null
    done
    "$JMP" admin create exporter -n "$NS" "$EXPORTER" \
        --label "$SELECTOR" --nointeractive --out "$WORK/exporter.yaml" >/dev/null
}

# Point the exporter at the proxy. The proxy verifies the controller with the
# exporter's original CA, and the exporter trusts that CA plus the proxy's, so
# its direct router connection is unchanged.
configure_exporter() {
    local cfg="$WORK/exporter.yaml" ca insecure bundle
    ca="$(yq '.tls.ca // ""' "$cfg")"
    insecure="$(yq '.tls.insecure // false' "$cfg")"
    if [ -n "$ca" ] && [ "${HOLDPROXY_UPSTREAM_INSECURE:-0}" != 1 ]; then
        printf '%s' "$ca" | base64 --decode >"$WORK/controller-ca.pem"
        PROXY_TLS=(-upstream-ca "$WORK/controller-ca.pem")
    else
        PROXY_TLS=(-upstream-insecure)
    fi

    start_proxy

    EP="$PROXY_ADDR" yq -i '.endpoint = strenv(EP)' "$cfg"
    F="$REPO_ROOT/e2e/exporters/exporter.yaml" yq -i '.export = load(strenv(F)).export' "$cfg"
    if [ "$insecure" = true ] || [ -z "$ca" ]; then
        log_warn "exporter config has no CA; using tls.insecure for controller and router"
        yq -i '.tls.insecure = true | .tls.ca = ""' "$cfg"
    else
        bundle="$({ printf '%s' "$ca" | base64 --decode; echo; cat "$WORK/proxy-ca.pem"; } | base64 | tr -d '\n')"
        B="$bundle" yq -i '.tls.ca = strenv(B)' "$cfg"
    fi
}

exporter_pattern() {
    echo "--exporter-config $WORK/exporter.yaml"
}

start_exporter() {
    "$JMP" run --exporter-config "$WORK/exporter.yaml" >>"$WORK/exporter.log" 2>&1 &
}

# `jmp run` forks and the child keeps the same argv, so match on the config path.
stop_exporter() {
    local i
    pkill -TERM -f -- "$(exporter_pattern)" 2>/dev/null || return 0
    for i in $(seq 1 30); do
        if ! pgrep -f -- "$(exporter_pattern)" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    log_warn "exporter still running after ${i}s, killing it"
    pkill -KILL -f -- "$(exporter_pattern)" 2>/dev/null || true
}

wait_registered() {
    kubectl -n "$NS" wait --timeout=120s --for=condition=Online --for=condition=Registered \
        "exporters.jumpstarter.dev/$EXPORTER" >/dev/null && wait_state "Available|" 60
}

ensure_idle() {
    if wait_state "Available|" 30; then
        return 0
    fi
    log_warn "exporter left as '$(exporter_state || true)'; restarting it to recover"
    stop_exporter
    start_exporter
    wait_registered
}

scenario_stale_available() {
    local a="rsf-$RUN-a1" b="rsf-$RUN-b1" held id result state bob_rc=0 observed=inconclusive
    step "Scenario stale-available: lease A's Available arrives after the exporter moved to lease B"

    say alice "leases the exporter and uses it"
    jmp_as alice create lease -l "$SELECTOR" --duration 30m --lease-id "$a" -o name
    LEASES+=("$a")
    if ! wait_state "LeaseReady|$a" 90 || ! jmp_as alice shell --lease "$a" -- j power on; then
        fail "alice could not use lease A"
        return 0
    fi

    say bob "queues for the same exporter"
    jmp_as bob create lease -l "$SELECTOR" --duration 30m --lease-id "$b" -o name
    LEASES+=("$b")

    admin POST "/arm?status=AVAILABLE" >/dev/null
    say alice "releases lease A; the exporter's Available report is delayed in transit"
    jmp_as alice delete leases "$a"
    if ! held="$(admin GET "/wait-held?timeout=60s")"; then
        fail "the exporter never sent Available for lease A"
        return 0
    fi
    id="$(hold_id "$held")"
    say proxy "holding $held until the exporter's 30s deadline passes"
    if ! admin GET "/wait-gone?id=$id&timeout=90s" >/dev/null; then
        fail "the exporter never gave up on the held report"
        admin POST "/drop?id=$id" >/dev/null || true
        return 0
    fi
    say proxy "exporter gave up and retried; waiting for the controller to hand the exporter to bob"
    if wait_state "LeaseReady|$b" 120; then
        pass "retried cleanup report accepted after LeaseRef cleared; lease B assigned"
    else
        fail "exporter was not reassigned to lease B (cleanup report rejected?)"
        show_lease "$b"
        admin POST "/drop?id=$id" >/dev/null || true
        return 0
    fi
    if ! jmp_as bob shell --lease "$b" -- j power on; then
        fail "bob could not use lease B before the late report"
        admin POST "/drop?id=$id" >/dev/null || true
        return 0
    fi

    say proxy "delivering lease A's Available to the controller now"
    result="$(admin POST "/release?id=$id")"
    say proxy "controller answered: $result"
    sleep 2
    state="$(exporter_state || true)"
    say kubectl "exporter status|leaseRef: $state"
    say bob "keeps working on lease B"
    jmp_as bob shell --lease "$b" --dial-timeout 20s -- j power on || bob_rc=$?

    if [[ $result == FailedPrecondition* && $state == "LeaseReady|$b" && $bob_rc == 0 ]]; then
        observed=fenced
    elif [[ $result == OK && $state == "Available|$b" && $bob_rc != 0 ]]; then
        observed=stale
    fi
    verdict "late Available after reassignment" "$observed"

    jmp_as bob delete leases "$b" || true
    ensure_idle
}

scenario_stale_lease_ready() {
    local a="rsf-$RUN-a2" c="rsf-$RUN-c2" held id result state charlie_rc=0 observed=inconclusive
    step "Scenario stale-lease-ready: lease A's LeaseReady arrives after lease A ended"

    admin POST "/arm?status=LEASE_READY" >/dev/null
    say alice "leases the exporter; the exporter's LeaseReady report is delayed in transit"
    jmp_as alice create lease -l "$SELECTOR" --duration 30m --lease-id "$a" -o name
    LEASES+=("$a")
    if ! held="$(admin GET "/wait-held?timeout=60s")"; then
        fail "the exporter never sent LeaseReady for lease A"
        return 0
    fi
    id="$(hold_id "$held")"
    say proxy "holding $held"
    say alice "connects; Dial waits until the exporter's retried LeaseReady lands (~30s)"
    if ! jmp_as alice shell --lease "$a" --dial-timeout 120s -- j power on; then
        fail "alice could not use lease A after the retried LeaseReady"
        admin POST "/drop?id=$id" >/dev/null || true
        return 0
    fi
    admin GET "/wait-gone?id=$id&timeout=30s" >/dev/null || true

    say alice "releases lease A"
    jmp_as alice delete leases "$a"
    if wait_state "Available|" 60; then
        pass "normal cleanup report accepted at lease end"
    else
        fail "exporter did not return to Available after lease A ended"
        admin POST "/drop?id=$id" >/dev/null || true
        return 0
    fi

    say proxy "delivering lease A's LeaseReady to the controller now"
    result="$(admin POST "/release?id=$id")"
    say proxy "controller answered: $result"
    sleep 2
    state="$(exporter_state || true)"
    say kubectl "exporter status|leaseRef: $state"

    say charlie "asks for the idle exporter"
    jmp_as charlie create lease -l "$SELECTOR" --duration 30m --lease-id "$c" -o name
    LEASES+=("$c")
    if wait_assigned "$c" 30; then
        jmp_as charlie shell --lease "$c" --dial-timeout 30s -- j power on || charlie_rc=$?
    else
        charlie_rc=1
        say charlie "lease $c is still unassigned after 30s:"
        show_lease "$c"
    fi

    if [[ $result == FailedPrecondition* && $state == "Available|" && $charlie_rc == 0 ]]; then
        observed=fenced
    elif [[ $result == OK && $state == "LeaseReady|" && $charlie_rc != 0 ]]; then
        observed=stale
    fi
    verdict "late LeaseReady after lease end" "$observed"

    jmp_as charlie delete leases "$c" || true
    ensure_idle
}

# shellcheck disable=SC2317 # invoked by the EXIT trap
cleanup() {
    local rc=$? lease
    set +e
    kubectl -n "$NS" logs deployment/jumpstarter-controller --since-time="$START_TIME" \
        >"$WORK/controller.log" 2>&1
    if [ "$KEEP" = 1 ]; then
        log_warn "--keep: exporter, proxy (pid ${PROXY_PID:-none}), leases and CRs left in place"
        log_info "logs: $WORK"
        exit "$rc"
    fi
    for lease in ${LEASES[@]+"${LEASES[@]}"}; do
        kubectl -n "$NS" delete "leases.jumpstarter.dev/$lease" --ignore-not-found --wait=false >/dev/null
    done
    stop_exporter
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null
    fi
    kubectl -n "$NS" delete --ignore-not-found --wait=false "exporters.jumpstarter.dev/$EXPORTER" \
        "clients.jumpstarter.dev/rsf-$RUN-alice" "clients.jumpstarter.dev/rsf-$RUN-bob" \
        "clients.jumpstarter.dev/rsf-$RUN-charlie" >/dev/null
    log_info "logs: $WORK"
    exit "$rc"
}
trap cleanup EXIT

log_info "namespace $NS, controller $ENDPOINT, expecting late reports to be $EXPECT"
if [ "$LEGACY" = 1 ]; then
    log_info "legacy exporter: lease_name is stripped from every ReportStatus"
fi

create_identities
configure_exporter

step "Startup: exporter registers through the proxy"
start_exporter
if wait_registered && proxy_log_has 'pass status=AVAILABLE .*-> OK$'; then
    pass "unleased startup report accepted"
else
    fail "exporter did not become Available through the proxy"
    tail -n 30 "$WORK/exporter.log" "$WORK/holdproxy.log" || true
    exit 1
fi

if [ "$SCENARIO" = all ] || [ "$SCENARIO" = stale-available ]; then
    scenario_stale_available
fi
if [ "$SCENARIO" = all ] || [ "$SCENARIO" = stale-lease-ready ]; then
    scenario_stale_lease_ready
fi

step "Shutdown: exporter stops without a lease"
stop_exporter
if proxy_log_has 'pass status=OFFLINE .*-> OK$'; then
    pass "unleased shutdown report accepted"
else
    fail "no accepted Offline report on shutdown"
fi

echo
echo "Summary (expected late reports: $EXPECT$([ "$LEGACY" = 1 ] && echo ', legacy exporter'))"
for line in "${RESULTS[@]}"; do
    echo "  $line"
done
echo "  ReportStatus traffic: $WORK/holdproxy.log"
exit "$FAILED"
