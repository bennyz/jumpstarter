// ReportStatus refinement: one exporter, two distinct, non-reused lease names,
// and three reports (unleased startup, A, B). Identity is now a wire field.
// A client deadline frees the sender but leaves the server operation runnable.
// Read/check and status patch are separate actions; a conflict retries the check.
// No assumption of serialized server completion, FIFO completion, or timely
// cancellation is made. This replaces the report-completion assumption only;
// it does not model hooks, pending lease handling, Dial, or informer convergence.
//
// Safety is at the status-write linearization point relative to LeaseRef. It is
// not a monotonic epoch property: idle cleanup may carry an older lease name,
// and reports within one lease have no sequence number. Legacy peers deliberately
// remain unfenced. The exporter lifecycle model's ghost highReport/NoStaleReport
// observation must not be interpreted as an implementation guarantee.
module ReportStatus {
  const Fence: bool
  const CheckVersion: bool
  const Recheck: bool
  pure val Reports = Set(0, 1, 2)
  pure val Statuses = Set("Available", "Offline", "LeaseReady", "AfterLeaseHook")
  type Report = { lease: int, status: str, phase: str, rv: int }
  type State = {
    assigned: int, issued: int, rv: int, reports: int -> Report,
    busy: int, timedOut: Set[int], reported: str, writeSafe: bool,
  }
  var s: State
  pure def allowed(lease, status, assigned) =
    if (assigned != 0) lease == assigned
    else if (lease == 0) status.in(Set("Available", "Offline"))
    else status.in(Set("Available", "Offline", "AfterLeaseHook"))

  action init = s' = {
    assigned: 0, issued: 0, rv: 0,
    reports: Reports.mapBy(i => { lease: i, status: "Available", phase: "new", rv: 0 }),
    busy: -1, timedOut: Set(), reported: "Available", writeSafe: true,
  }
  action Assign = all {
    s.assigned == 0, s.issued < 2, s.reported == "Available",
    s' = { ...s, assigned: s.issued + 1, issued: s.issued + 1, rv: s.rv + 1 },
  }
  action End = all {
    s.assigned != 0,
    s' = { ...s, assigned: 0, rv: s.rv + 1 },
  }
  // Capture identity while the local lease matches. Subsequent transport and
  // Kubernetes retries retain this immutable report, even after reassignment.
  action Enqueue(i, status) = all {
    s.reports.get(i).phase == "new", s.assigned == i,
    s' = { ...s, reports: s.reports.set(i, { ...s.reports.get(i), status: status, phase: "queued" }) },
  }
  action Send(i) = all {
    s.reports.get(i).phase == "queued", s.busy == -1,
    s' = { ...s, busy: i, reports: s.reports.set(i, { ...s.reports.get(i), phase: "sent" }) },
  }
  action Timeout(i) = all {
    s.busy == i, s.reports.get(i).phase != "done",
    s' = { ...s, busy: -1, timedOut: s.timedOut.union(Set(i)) },
  }
  action Read(i) = all {
    s.reports.get(i).phase.in(Set("sent", "retry")),
    {
      val r = s.reports.get(i)
      val accepted = not(Fence) or (not(Recheck) and r.phase == "retry") or allowed(r.lease, r.status, s.assigned)
      s' = { ...s, reports: s.reports.set(i, { ...r, rv: s.rv, phase: if (accepted) "checked" else "done" }) }
    },
  }
  action Write(i) = all {
    s.reports.get(i).phase == "checked",
    {
      val r = s.reports.get(i)
      if (CheckVersion and r.rv != s.rv)
        s' = { ...s, reports: s.reports.set(i, { ...r, phase: "retry" }) }
      else s' = { ...s, rv: s.rv + 1, reported: r.status,
        reports: s.reports.set(i, { ...r, phase: "done" }),
        writeSafe: s.writeSafe and allowed(r.lease, r.status, s.assigned) }
    },
  }
  action Reply(i) = all {
    s.busy == i, s.reports.get(i).phase == "done",
    s' = { ...s, busy: -1 },
  }
  action step = any {
    Assign, End,
    { nondet i = Reports.oneOf(); nondet status = Statuses.oneOf(); any {
      Enqueue(i, status), Send(i), Timeout(i), Read(i), Write(i), Reply(i),
    } },
  }
  val TypeOK = all {
    s.assigned.in(0.to(2)), s.issued.in(0.to(2)), s.rv.in(0.to(7)),
    s.busy.in((-1).to(2)), s.timedOut.subseteq(Reports), s.reported.in(Statuses),
    s.reports.keys() == Reports,
    Reports.forall(i => s.reports.get(i).lease == i),
  }
  val NoStaleWrite = s.writeSafe

  // A successful new report is possible after conflict, provided the lease is
  // still valid. No wall-time or eventual delivery promise is made here.
  run cleanupAfterRefClearedTest = init.then(Assign).then(Enqueue(1, "Available"))
    .then(Send(1)).then(Read(1)).then(End).then(Write(1)).then(Read(1)).then(Write(1))
    .then(all { assert(s.reported == "Available" and s.reports.get(1).phase == "done" and NoStaleWrite), s' = s })

  run timeoutThenReassignTest = init.then(Assign).then(Enqueue(1, "Available"))
    .then(Send(1)).then(Read(1)).then(Timeout(1)).then(End).then(Assign)
    .then(Enqueue(2, "LeaseReady")).then(Send(2)).then(Read(2)).then(Write(2))
    .then(Write(1)).then(Read(1))
    .then(all { assert(s.reported == "LeaseReady" and s.reports.get(1).phase == "done" and NoStaleWrite), s' = s })
}

module Fenced { import ReportStatus(Fence = true, CheckVersion = true, Recheck = true).* }
module Legacy { import ReportStatus(Fence = false, CheckVersion = true, Recheck = true).* }
module NoResourceVersion { import ReportStatus(Fence = true, CheckVersion = false, Recheck = true).* }
module NoRetryCheck { import ReportStatus(Fence = true, CheckVersion = true, Recheck = false).* }
