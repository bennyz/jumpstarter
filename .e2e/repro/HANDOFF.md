# Handoff: run the ReportStatus fencing reproducer on local Kind

Run the reproducer in this worktree against both this branch's controller and
main's, and report the results. Don't change controller or exporter code to
make it pass; fix only the reproducer (`e2e/repro/*`, `controller/test/repro/*`)
if it is wrong.

## Background

ReportStatus now carries `lease_name`, and the controller rejects a report
whose lease no longer owns the exporter (`checkReportLease` in
`controller/internal/service/controller_service.go`). On a healthy Kind cluster
a late report can't occur naturally: the exporter's cancellation always reaches
the controller before the late handler writes. So the reproducer injects one
fault: `controller/test/repro/reportstatus-holdproxy` sits between the exporter
and the controller, holds one ReportStatus past the exporter's 30s deadline,
then delivers it without the cancellation. The controller, exporter and `jmp`
commands are unmodified.

Already verified outside Kind: the proxy builds with grpc v1.80.0 / protobuf
v1.36.11, and a functional test against a fake controller (17 checks: ordering,
fresh deadline, auth metadata, fenced/unfenced results, drop, streams,
keepalive, `-strip-lease-name`, both TLS trust paths) passed. The `yq` config
edits were tested with yq v4.53.6. Nothing has run on Kind yet.

## Steps

0. Check `.e2e-setup-complete` exists in the worktree root. If not, stop and
   ask the user to run `make e2e-setup`; it needs sudo for `/etc/hosts`.
1. `(cd controller && go build ./test/repro/...)`
2. `chmod +x e2e/repro/*.sh && e2e/repro/report-status-fencing.sh`
3. Old controller:
   - `git worktree add ../jumpstarter-main main` (if no main checkout exists)
   - `e2e/repro/swap-controller.sh ../jumpstarter-main`
   - `e2e/repro/report-status-fencing.sh --expect stale`
   - `e2e/repro/swap-controller.sh .` to restore this branch's controller
4. Optional: `e2e/repro/report-status-fencing.sh --legacy-exporter` on this
   branch's controller (expects stale: reports without `lease_name` stay
   unfenced by design).

## Expected

| Run | Late Available | Late LeaseReady |
|---|---|---|
| This branch | `FailedPrecondition`, exporter stays `LeaseReady|<bob's lease>`, bob's shell works | `FailedPrecondition`, exporter stays `Available|`, charlie gets the exporter |
| Main | `OK`, exporter reads `Available|<bob's lease>`, bob's shell fails "not ready" | `OK`, idle exporter reads `LeaseReady|`, charlie's lease stays pending |

Every run must also PASS: unleased startup and shutdown reports, the retried
cleanup report after LeaseRef cleared (bob gets lease B), and normal cleanup.

## If something fails

Logs are in `.e2e/repro/report-status-fencing-<run>/`: `holdproxy.log` (every
ReportStatus and its result), `exporter.log`, `controller.log`.

- Exporter never becomes Available: check `holdproxy.log` for upstream TLS
  errors; retry with `HOLDPROXY_UPSTREAM_INSECURE=1`.
- Ports 18082/18090 busy: set `PROXY_ADDR` / `ADMIN_ADDR`.
- Late release prints `Internal: ...` on this branch: likely conflict-retry
  exhaustion in ReportStatus (cached `Get` + optimistic lock); report it rather
  than working around it.
- `--keep` leaves the exporter, proxy and leases up for inspection.

## Report back

The summary block from each run, and for any failure the relevant lines from
the three logs.
