"""Exporter lease lifecycle orchestration.

One loop admits leases. Each lease is one call to ``ExporterLifecycle._serve_lease``,
whose session and client connections are ``async with`` blocks: leaving them closes
the lease's resources, on return, error or cancellation, before the next lease can
be admitted. Every wait goes through ``InputBuffer.wait_until``, so every decision
reads live inputs.

``_lease_activity`` is the lease from top to bottom: set up, serve, clean up, hold.
``_run_hook`` is what running a hook means for both hooks: its record, its status
reports and its onFailure. ``HookExecutor`` only runs the script.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from typing import TYPE_CHECKING, assert_never

import anyio
from anyio import CancelScope, Event, create_memory_object_stream, create_task_group, get_cancelled_exc_class
from jumpstarter_protocol import jumpstarter_pb2

from jumpstarter.common import HOOK_WARNING_PREFIX, ExporterStatus, LeaseHookPhase
from jumpstarter.exporter.inputs import Assignment, InputBuffer, Inputs
from jumpstarter.exporter.lease_context import LeaseContext
from jumpstarter.exporter.lease_hooks import (
    FINISHED_HOOK_PHASES,
    HOOK_NAMES,
    HookPlan,
    HookType,
    RecordState,
    Restart,
    decide_restart,
    resumed_status,
)
from jumpstarter.exporter.phase import Phase
from jumpstarter.logging import clear_log_context, set_log_context

if TYPE_CHECKING:
    from jumpstarter.exporter.exporter import Exporter

logger = logging.getLogger(__name__)


def _hook_failure_ended_lease(state: jumpstarter_pb2.LeaseHookState | None) -> bool:
    """Whether a recorded hook failed with an onFailure action that releases the lease."""
    return (
        state is not None
        and state.phase == LeaseHookPhase.FAILED
        and state.on_failure == jumpstarter_pb2.LEASE_HOOK_FAILURE_ACTION_END_LEASE
    )


class Entry(Enum):
    """Where a lease's activity starts. Only a restart starts anywhere but NEW."""

    NEW = auto()  # set the lease up
    RESUME = auto()  # an earlier process set it up: no setup, no device reset
    FINISH_SETUP = auto()  # an earlier process's setup was cut off: rerun or fail it, per onInterrupt
    CLEAN_UP = auto()  # it owes cleanup: run it first thing

    @property
    def runs_setup(self) -> bool:
        """Whether this process runs the lease's setup; if not, an earlier one did."""
        return self in (Entry.NEW, Entry.FINISH_SETUP)


class SetupOutcome(Enum):
    """What a lease needs once its setup is settled."""

    CLEAN_UP = auto()
    """Cleanup when the lease ends: setup ran, warned or failed unexpectedly."""
    NO_CLEANUP = auto()
    """No cleanup, only holding the lease until it is released: setup failed with
    endLease or exit, or never started in a process that set nothing up."""


class HookOutcome(Enum):
    """How a hook ended: whether it failed, and what its onFailure then asks for."""

    SUCCEEDED = auto()
    WARN = auto()  # failed with onFailure: warn; the lease goes on
    END_LEASE = auto()  # failed with onFailure: endLease; the lease is released
    EXIT = auto()  # failed with onFailure: exit; the exporter stops once the lease is released
    ERROR = auto()  # the exporter failed to run it: no onFailure applies


class LeaseEnd(Enum):
    """Why a lease stopped being served."""

    END_SESSION = auto()  # its client asked: clean up while the client is still connected
    ENDED = auto()  # released, revoked or expired, or the exporter stops: clean up without clients
    HANDED_OFF = auto()  # the exporter stops and the lease is recorded: the next process serves it


@dataclass(frozen=True)
class _HookReports:
    """The statuses a hook reports."""

    running: tuple[ExporterStatus, str]
    done: tuple[ExporterStatus, str]
    failed: ExporterStatus


_REPORTS: dict[HookType, _HookReports] = {
    "before_lease": _HookReports(
        running=(ExporterStatus.BEFORE_LEASE_HOOK, "Running beforeLease hook"),
        done=(ExporterStatus.LEASE_READY, "Ready for commands"),
        failed=ExporterStatus.BEFORE_LEASE_HOOK_FAILED,
    ),
    "after_lease": _HookReports(
        running=(ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks"),
        done=(ExporterStatus.AVAILABLE, "Available for new lease"),
        failed=ExporterStatus.AFTER_LEASE_HOOK_FAILED,
    ),
}


def _done_report(hook: HookType, warning: str) -> tuple[ExporterStatus, str]:
    """What a hook that succeeded, or failed with onFailure: warn, reports."""
    status, message = _REPORTS[hook].done
    if warning:
        message = f"{HOOK_WARNING_PREFIX}{HOOK_NAMES[hook]} hook warning: {warning}"
    return status, message


_ENTRY_PHASES: dict[Entry, Phase] = {
    Entry.NEW: Phase.SETUP,
    Entry.FINISH_SETUP: Phase.SETUP,
    Entry.RESUME: Phase.SERVING,
    Entry.CLEAN_UP: Phase.CLEANUP,
}


@asynccontextmanager
async def stoppable(inputs: InputBuffer, stop_grace: float, what: str) -> AsyncGenerator[None, None]:
    """Run a hook, or a record write, so that a stop-now cuts it off after stop_grace."""
    with CancelScope() as scope:
        async with create_task_group() as tg:

            async def arm() -> None:
                snap = await inputs.wait_until(lambda i: i.stop_now)
                assert snap.stop is not None and snap.stop.now_at is not None
                scope.deadline = snap.stop.now_at + stop_grace
                left = scope.deadline - anyio.current_time()
                logger.info("Stop requested: %s may run %.1fs more", what, max(left, 0.0))

            tg.start_soon(arm)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
    if scope.cancelled_caught:
        logger.warning("%s cut off after the %.1fs stop grace", what, stop_grace)


class StatusSink:
    """Where the Status stream writes: purely into InputBuffer, never mutating lease state."""

    def __init__(self, inputs: InputBuffer) -> None:
        self._inputs = inputs

    async def send(self, status: jumpstarter_pb2.StatusResponse) -> None:
        inputs = self._inputs.apply_status(status)
        current = inputs.assignment
        if current is None:
            logger.info("Currently not leased")
        else:
            logger.info("Currently leased by %s under %s", current.client_name, current.lease_name)


class ExporterLifecycle:
    """Sequential lease lifecycle controller for one Exporter."""

    def __init__(self, exporter: Exporter) -> None:
        self.exporter = exporter

    @property
    def _inputs(self) -> InputBuffer:
        return self.exporter._inputs

    @property
    def _phase(self):
        return self.exporter._phase

    async def run(self) -> None:
        """Run the controller-mode lease admission loop until stopped."""
        inputs = await self._inputs.wait_until(lambda i: i.status_seen or i.stop is not None)
        admitted, parked = None, None
        if inputs.stop is None:
            admitted, parked = await self._take_over_from_previous_process(inputs)
        while parked is None:
            inputs = await self._inputs.wait_until(
                lambda i, admitted=admitted: i.stop is not None
                or (i.assignment is not None and i.generation != admitted)
            )
            if inputs.stop is not None:
                break
            assert inputs.assignment is not None
            admitted = inputs.generation
            a = inputs.assignment
            lease = LeaseContext(
                lease_name=a.lease_name,
                lease_uid=a.lease_uid,
                before_lease_hook=Event(),
                client_name=a.client_name,
                # Hook records identify a lease by its UID, which every controller that keeps them sends.
                hooks=HookPlan(
                    record=(
                        RecordState.TRACKED
                        if self.exporter._tracks_lease_hooks and a.lease_uid
                        else RecordState.UNTRACKED
                    )
                ),
            )
            parked = await self._serve_lease(lease, Entry.NEW, self._lease_log_context(a), generation=admitted)
        if parked is not None:
            await self._park(parked)
        self._phase.move(Phase.STOPPING)

    async def run_standalone(self, lease: LeaseContext) -> None:
        """Run one standalone TCP lease through the same lease activity until stopped."""
        try:
            self._phase.move(Phase.SETUP)
            # Standalone serves one lease until stopped: there is nothing to park.
            await self._lease_activity(lease, Entry.NEW, generation=None)
        finally:
            self._phase.move(Phase.IDLE)

    def _sync_client(self, lease: LeaseContext, generation: int | None) -> None:
        if generation is None:
            return
        client = self._inputs.client_for(generation)
        if client and client != lease.client_name:
            lease.update_client(client)
            set_log_context(client=client)

    async def _wait_lease(
        self, lease: LeaseContext, generation: int | None, predicate: Callable[[Inputs], bool]
    ) -> Inputs:
        def check(inputs: Inputs) -> bool:
            self._sync_client(lease, generation)
            return predicate(inputs)

        return await self._inputs.wait_until(check)

    async def _take_over_from_previous_process(self, inputs: Inputs) -> tuple[int | None, str | None]:
        """Pick up what a previous exporter process left of its last lease, before anything else.

        The controller's hook record names that lease, and decide_restart says
        what it still needs. A lease that needs hooks goes through _serve_lease
        like any other, with an Entry that skips the steps that already ran.
        Returns the assignment generation this handled, so the loop does not
        admit that lease again (None if the assigned lease, if any, is new),
        and why to park, as _serve_lease does.
        """
        exp = self.exporter
        record = inputs.hook_record
        a = inputs.assignment
        assigned = a.key if a is not None else None
        restart = decide_restart(record, assigned) if exp._tracks_lease_hooks and record is not None else None
        if restart in (None, Restart.NEW, Restart.NOTHING):
            if restart is Restart.NEW and record is not None and record.HasField("before_lease"):
                logger.info(
                    "Not finishing hooks of lease %s (uid %s): lease %s (uid %s) is assigned now",
                    record.lease_name, record.lease_uid, *(assigned or ("", "")),
                )
            if exp._records_lease_hooks and a is None:
                # Reported once the record is checked: it tells the controller no hook work is owed.
                await exp._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")
            return None, None
        assert record is not None
        before = record.before_lease
        after = record.after_lease if record.HasField("after_lease") else None
        lease = LeaseContext(
            lease_name=record.lease_name,
            lease_uid=record.lease_uid,
            before_lease_hook=Event(),
            hooks=HookPlan(record=RecordState.RECORDED),
            client_name=record.client_name,  # kept by the record, as the lease may have ended
        )
        handled = inputs.generation if assigned is not None else None
        if restart is Restart.SETTLE:
            await self._settle_lease_without_hooks(lease, before, after, assigned is not None, generation=handled)
            return handled, None
        if restart is Restart.FINISH_SETUP:
            logger.info("Resuming lease %s after exporter restart (beforeLease hook did not finish)", lease.lease_name)
            lease.hooks.finish_cut_off("before_lease", before, exp._on_interrupt("before_lease"), lease.lease_name)
            entry = Entry.FINISH_SETUP
        elif restart is Restart.RESUME:
            logger.info(
                "Resuming lease %s after exporter restart (beforeLease hook %s before it)",
                lease.lease_name, LeaseHookPhase(before.phase).name.lower(),
            )
            lease.update_status(*resumed_status(before))
            entry = Entry.RESUME
        elif restart is Restart.CLEAN_UP:
            await self._plan_owed_cleanup(lease, before, after, ended=assigned is None)
            entry = Entry.CLEAN_UP
        else:
            assert_never(restart)
        parked = await self._serve_lease(
            lease, entry, {"lease_id": lease.lease_name, "exporter": exp.exporter_name}, generation=handled,
        )
        return handled, parked

    async def _plan_owed_cleanup(
        self,
        lease: LeaseContext,
        before: jumpstarter_pb2.LeaseHookState,
        after: jumpstarter_pb2.LeaseHookState | None,
        *,
        ended: bool,
    ) -> None:
        """Prepare a recorded lease that owes its afterLease hook to start in cleanup."""
        exp = self.exporter
        if before.phase not in FINISHED_HOOK_PHASES:
            # The lease ended before the cut-off setup could run again.
            await exp._record_lease_hook(
                lease, "before_lease", LeaseHookPhase.FAILED,
                "beforeLease hook did not finish: the lease ended while the exporter was restarting",
                attempts=max(before.attempts, 1),
            )
        if after is not None:
            lease.hooks.finish_cut_off("after_lease", after, exp._on_interrupt("after_lease"), lease.lease_name)
        lease.update_status(*_REPORTS["after_lease"].running)
        if ended:
            logger.info("Lease %s ended while the exporter was down; running its afterLease hook", lease.lease_name)
            lease.lease_ended.set()
        else:
            logger.info("Lease %s owes its afterLease hook; running it", lease.lease_name)

    async def _settle_lease_without_hooks(
        self,
        lease: LeaseContext,
        before: jumpstarter_pb2.LeaseHookState,
        after: jumpstarter_pb2.LeaseHookState | None,
        assigned: bool,
        *,
        generation: int | None,
    ) -> None:
        """Settle a recorded lease that needs no more hooks.

        A setup that failed with endLease or exit skips cleanup, as without a
        restart. A lease still assigned is never set up again, and is released
        only if a hook failed with onFailure: endLease, as the previous process
        would have done; otherwise its client or its expiry ends it.
        """
        exp = self.exporter
        if after is None:
            await exp._record_lease_hook(lease, "after_lease", LeaseHookPhase.SKIPPED, "beforeLease hook failed")
        if not assigned:
            # Reported once the record is checked: it tells the controller no hook work is owed.
            await exp._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")
            return
        if generation is not None:
            lease.controller_ended = lambda gen=generation: not self._inputs.snapshot().holds(gen)
        exp._lease_context = lease  # routes the release and status below to this lease
        try:
            if _hook_failure_ended_lease(before) or _hook_failure_ended_lease(after):
                logger.info("Lease %s had a hook fail with onFailure: endLease; releasing it", lease.lease_name)
                await exp._request_lease_release(lease)  # reports AVAILABLE once released
            else:
                logger.info("Lease %s is done with its hooks; leaving it to its client", lease.lease_name)
                await exp._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        finally:
            exp._lease_context = None

    def _lease_log_context(self, assignment: Assignment) -> dict[str, str]:
        """Log context for a newly-assigned lease: its name, this exporter, and the lease's spec.context."""
        return {"lease_id": assignment.lease_name, "exporter": self.exporter.exporter_name, **dict(assignment.context)}

    async def _serve_lease(
        self, lease: LeaseContext, entry: Entry, log_context: dict[str, str], *, generation: int | None
    ) -> str | None:
        """Serve one lease, from setup to the controller releasing it.

        Returns why to park: the afterLease hook failed unexpectedly, so the
        device state is unknown. None once the device is ready for the next lease.
        """
        exp = self.exporter
        if generation is not None:
            lease.controller_ended = lambda gen=generation: not self._inputs.snapshot().holds(gen)
        exp._lease_context = lease
        set_log_context(**log_context)
        if lease.client_name:
            set_log_context(client=lease.client_name)
        logger.info("Starting %s lease: %s", entry.name.lower().replace("_", " "), lease.lease_name)
        if exp.exit_on_lease_end:
            # This is the last lease: drain now, so nothing reports AVAILABLE again.
            self._inputs.request_stop(now=False, unregister=True)
        try:
            self._phase.move(_ENTRY_PHASES[entry])
            # A lease an earlier process set up keeps the device as that process left it.
            async with exp.session_for_lease(reset_device=entry.runs_setup) as (
                session, main_path, hook_path,
            ):
                lease.session = session
                lease.socket_path = main_path
                lease.hook_socket_path = hook_path
                lease.notify = self._inputs.notify
                session.lease_context = lease
                session.update_status(lease.current_status, lease.status_message)
                parked = await self._lease_activity(lease, entry, generation=generation)
        finally:
            lease.before_lease_hook.set()
            exp._lease_context = None
            clear_log_context()
            set_log_context(exporter=exp.exporter_name)
            self._phase.move(Phase.IDLE)
        if parked is not None:
            return parked
        # Ready for the next lease. This also clears a failure status a lease
        # ended with (an endLease failure the controller ended it for); a stopping
        # exporter does not report it (_report_status).
        if exp._exporter_status != ExporterStatus.AVAILABLE:
            await exp._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        return None

    async def _park(self, reason: str) -> None:
        """Admit no lease until restarted: the afterLease hook failed unexpectedly.

        The device state is unknown, so the next lease could start on a board in
        any state. The failure status stays, which keeps the controller from
        assigning this exporter. A restart (after checking the device) clears it.
        """
        self._phase.move(Phase.PARKED)
        logger.error(
            "Not admitting leases: the afterLease hook failed unexpectedly (%s). "
            "Check the device, then restart the exporter.", reason,
        )
        await self.exporter._report_status(
            ExporterStatus.AFTER_LEASE_HOOK_FAILED,
            f"afterLease hook failed: {reason}; not admitting leases until the exporter restarts",
        )
        await self._inputs.wait_until(lambda i: i.stop is not None)

    async def _lease_activity(self, lease: LeaseContext, entry: Entry, *, generation: int | None) -> str | None:
        """Set up, serve and clean up one lease, then hold it until it is released.

        Returns why to park: the afterLease hook failed unexpectedly. Cancelled
        (a fatal stream error, or a forced stop) before cleanup started, it
        still cleans up within the stop grace.
        """
        exp = self.exporter
        self._sync_client(lease, generation)
        if entry is Entry.NEW and lease.has_ended():
            logger.info("Lease %s already ended, skipping", lease.lease_name)
            await exp._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
            return None

        owes_cleanup = True  # until cleanup starts, or setup leaves none owed
        try:
            # Standalone clients connect to the session directly, and a lease that
            # ended while no process served it has no clients to connect.
            async with self._lease_connections(lease, enabled=not exp._standalone and not lease.has_ended()):
                if entry.runs_setup:
                    owes_cleanup = await self._set_up(lease) is SetupOutcome.CLEAN_UP
                    if not owes_cleanup:
                        await self._hold_without_cleanup(lease, generation)
                        return None
                else:
                    # An earlier process ran this lease's setup: report where it left the lease.
                    await exp._report_status(lease.current_status, lease.status_message)
                    lease.before_lease_hook.set()

                if entry is Entry.CLEAN_UP:
                    # Cleanup is owed already. If the lease is still assigned it runs as
                    # after EndSession, with the lease's clients connected.
                    end = LeaseEnd.ENDED if lease.has_ended() else LeaseEnd.END_SESSION
                else:
                    end = await self._serve(lease, generation)

                match end:
                    case LeaseEnd.HANDED_OFF:
                        return None
                    case LeaseEnd.END_SESSION:
                        owes_cleanup = False
                        return await self._clean_up_and_hold(lease, generation)
                    case LeaseEnd.ENDED:
                        pass  # cleaned up below, once its clients are disconnected
                    case _:
                        assert_never(end)

            # Released, revoked or stopping: client connections are closed before
            # cleanup touches the device.
            owes_cleanup = False
            return await self._clean_up_and_hold(lease, generation)
        except get_cancelled_exc_class():
            if owes_cleanup:
                await self._clean_up_on_cancel(lease, generation)
            raise

    async def _serve(self, lease: LeaseContext, generation: int | None) -> LeaseEnd:
        """Serve the lease's clients until something ends that. Returns what did."""

        def over(inputs: Inputs) -> bool:
            return lease.has_ended() or inputs.stop_now or lease.end_session_requested.is_set()

        self._sync_client(lease, generation)
        if not over(self._inputs.snapshot()):
            self._phase.move(Phase.SERVING)
        inputs = await self._wait_lease(lease, generation, over)
        if lease.has_ended():
            return LeaseEnd.ENDED
        if not inputs.stop_now:
            logger.info("EndSession requested, running afterLease hook")
            return LeaseEnd.END_SESSION
        if lease.hooks.tracked:
            # A restart is a process event, not a lease event: the lease and its
            # device stay as they are, and the next process resumes it from the record.
            logger.info("Stopping: leaving lease %s to the next exporter process", lease.lease_name)
            return LeaseEnd.HANDED_OFF
        return LeaseEnd.ENDED

    async def _set_up(self, lease: LeaseContext) -> SetupOutcome:
        """Set the lease up, within the stop grace once a stop is requested.

        Clients and driver calls wait until setup is settled, whichever way.
        """
        try:
            async with stoppable(self._inputs, self.exporter.stop_grace, "beforeLease hook"):
                return await self._run_before_lease_hook(lease)
            return SetupOutcome.CLEAN_UP  # cut off by the stop grace: cleanup still runs
        finally:
            lease.before_lease_hook.set()

    async def _run_before_lease_hook(self, lease: LeaseContext) -> SetupOutcome:
        exp = self.exporter
        # A lease can end before its setup starts: one of a backlog the exporter
        # could not keep up with, or one whose setup an earlier process started.
        if lease.has_ended():
            logger.info("Lease %s already ended, skipping beforeLease hook", lease.lease_name)
            return await self._setup_did_not_start(lease)
        if exp.hook_executor is None or exp.hook_executor.config.before_lease is None:
            if not await exp._record_lease_hook(lease, "before_lease", LeaseHookPhase.SKIPPED):
                logger.warning("Not making lease %s ready: its start was not recorded", lease.lease_name)
                return SetupOutcome.NO_CLEANUP
            await exp._report_status(*_REPORTS["before_lease"].done)
            return SetupOutcome.CLEAN_UP
        if not await self._setup_may_start(lease):
            return await self._setup_did_not_start(lease)

        outcome, message = await self._run_hook("before_lease", lease)
        match outcome:
            case HookOutcome.SUCCEEDED | HookOutcome.WARN:
                if lease.has_ended():
                    logger.info(
                        "Lease %s ended during beforeLease hook, skipping LEASE_READY transition", lease.lease_name
                    )
                else:
                    await exp._report_status(*_done_report("before_lease", message))
                return SetupOutcome.CLEAN_UP
            case HookOutcome.ERROR:
                # The lease stays on its failure status; whatever setup did is still cleaned up.
                return SetupOutcome.CLEAN_UP
            case HookOutcome.END_LEASE:
                # Shielded: a stop must not leave the lease to a setup that failed.
                with CancelScope(shield=True):
                    await self._release_after_failure(lease)
                return SetupOutcome.NO_CLEANUP
            case HookOutcome.EXIT:
                return SetupOutcome.NO_CLEANUP
            case _:
                assert_never(outcome)

    async def _setup_may_start(self, lease: LeaseContext) -> bool:
        """Record that setup starts. False if it must not run."""
        if not await self._record_start("before_lease", lease):
            logger.warning(
                "Not running beforeLease hook for lease %s: its start was not recorded", lease.lease_name
            )
            return False
        # Recording can wait on the controller while the lease ends. A late
        # acknowledgement must not run setup.
        return not lease.has_ended()

    async def _setup_did_not_start(self, lease: LeaseContext) -> SetupOutcome:
        """Settle a setup that never ran in this process.

        Only a previous process's setup needs cleanup. Its record is closed
        either way: the latest start may have been stored without a reply, and
        if it was not, an interrupted earlier attempt may still be recorded as
        running. The controller accepts an outcome only for the running attempt.
        """
        outcome = SetupOutcome.CLEAN_UP if lease.hooks.recorded else SetupOutcome.NO_CLEANUP
        record = partial(self.exporter._record_lease_hook, lease, "before_lease", LeaseHookPhase.FAILED)
        message = "beforeLease hook did not start before its lease ended"
        attempt = lease.hooks.attempt("before_lease")
        if not await record(message, attempts=attempt):
            previous = lease.hooks.started.get("before_lease")
            if previous is not None and previous != attempt:
                await record(message, attempts=previous)
        return outcome

    async def _clean_up(self, lease: LeaseContext, generation: int | None) -> str | None:
        """Clean up after the lease. Returns why the device state is unknown, if it is."""
        exp = self.exporter
        self._sync_client(lease, generation)
        self._phase.move(Phase.CLEANUP)
        used = lease.has_client() or exp._standalone
        if exp.hook_executor is None or exp.hook_executor.config.after_lease is None or not used:
            await exp._record_lease_hook(
                lease, "after_lease", LeaseHookPhase.SKIPPED, "" if used else "no client used the lease"
            )
            await exp._report_status(*_REPORTS["after_lease"].done)
            return None
        logger.info("Running afterLease hook")
        async with stoppable(self._inputs, exp.stop_grace, "afterLease hook"):
            if not await self._record_start("after_lease", lease):
                # Another lease has the board now, or the start could not be recorded and
                # the next exporter process runs the hook; this one must not touch the board.
                logger.warning(
                    "Not running afterLease hook for lease %s: its start was not recorded", lease.lease_name
                )
                await exp._report_status(*_REPORTS["after_lease"].done)
                return None
            outcome, message = await self._run_hook("after_lease", lease)
            match outcome:
                case HookOutcome.SUCCEEDED | HookOutcome.WARN:
                    await exp._report_status(*_done_report("after_lease", message))
                    return None
                case HookOutcome.ERROR:
                    return message  # the device is in an unknown state
                case HookOutcome.END_LEASE:
                    await self._release_after_failure(lease)
                    return None
                case HookOutcome.EXIT:
                    return None
                case _:
                    assert_never(outcome)
        return None  # cut off by the stop grace

    async def _record_start(self, hook: HookType, lease: LeaseContext) -> bool:
        """Record that the hook starts. False if the controller refused: it must not run.

        A hook that a restart cut off, and that fails instead of running again,
        is recorded as running already.
        """
        return hook in lease.hooks.cut_off or await self.exporter._record_lease_hook(
            lease, hook, LeaseHookPhase.RUNNING
        )

    async def _run_hook(self, hook: HookType, lease: LeaseContext) -> tuple[HookOutcome, str]:
        """Run a hook whose start is recorded, then record and report how it ended.

        Returns how it ended and, if it failed, the failure message. What a
        failure means for the exporter is applied here; the caller applies what
        it means for the lease, and reports a hook that succeeded or only warned.
        """
        exp = self.exporter
        assert exp.hook_executor is not None
        name, reports = HOOK_NAMES[hook], _REPORTS[hook]
        record = partial(exp._record_lease_hook, lease, hook)
        await exp._report_status(*reports.running)
        try:
            if hook == "before_lease":
                failure = await exp.hook_executor.execute_before_lease_hook(lease)
            else:
                failure = await exp.hook_executor.execute_after_lease_hook(lease)
        except Exception as e:  # noqa: BLE001
            # Not the script failing but the exporter failing to run it.
            logger.error("%s hook failed with unexpected error: %s", name, e)
            await record(LeaseHookPhase.FAILED, str(e))
            await exp._report_status(reports.failed, f"{name} hook failed: {e}")
            return HookOutcome.ERROR, str(e)
        if failure is None:
            await record(LeaseHookPhase.SUCCEEDED)
            return HookOutcome.SUCCEEDED, ""

        message = failure.message
        match failure.on_failure:
            case "warn":
                await record(LeaseHookPhase.FAILED, message, "warn")
                return HookOutcome.WARN, message
            case "exit":
                logger.error("%s hook failed with on_failure='exit': %s", name, message)
                await record(LeaseHookPhase.FAILED, message, "exit")
                await exp._report_status(
                    reports.failed, f"{name} hook failed (on_failure=exit, shutting down): {message}"
                )
                await exp._report_status(
                    ExporterStatus.OFFLINE, f"Exporter shutting down due to {name} hook failure"
                )
                # The exporter stops once this lease is released. Exit code 1
                # tells the CLI not to restart it.
                exp.stop(exit_code=1, wait_for_lease_exit=True, should_unregister=True)
                return HookOutcome.EXIT, message
            case "endLease":
                logger.error("%s hook failed with on_failure='endLease': %s", name, message)
                # Reported before it is recorded, so that the client sees the failure:
                # a controller that keeps the record ends the lease as soon as it is recorded.
                await exp._report_status(reports.failed, f"{name} hook failed (on_failure=endLease): {message}")
                await record(LeaseHookPhase.FAILED, message, "endLease")
                return HookOutcome.END_LEASE, message
            case unknown:
                assert_never(unknown)

    async def _release_after_failure(self, lease: LeaseContext) -> None:
        """Release a lease whose hook failed with onFailure: endLease."""
        await anyio.sleep(1.0)  # the client gets time to see the failure first
        await self.exporter._request_lease_release(lease)

    async def _hold_without_cleanup(self, lease: LeaseContext, generation: int | None) -> None:
        """Hold a lease whose setup leaves no cleanup, keeping its failure status until release.

        The record's cleanup is closed first, or the controller holds this exporter for it.
        """
        if lease.hooks.recorded:
            await self.exporter._record_lease_hook(
                lease, "after_lease", LeaseHookPhase.SKIPPED, "beforeLease hook failed"
            )
        await self._hold(lease, generation)

    async def _clean_up_and_hold(self, lease: LeaseContext, generation: int | None) -> str | None:
        unknown_state = await self._clean_up(lease, generation)
        await self._hold(lease, generation)
        return unknown_state

    async def _hold(self, lease: LeaseContext, generation: int | None) -> None:
        """Keep the lease's session until the controller releases the lease, or a stop."""
        if lease.has_ended() or self._inputs.snapshot().stop_now:
            return
        self._phase.move(Phase.HOLDING)
        await self._wait_lease(lease, generation, lambda i: lease.has_ended() or i.stop_now)

    async def _clean_up_on_cancel(self, lease: LeaseContext, generation: int | None) -> None:
        """The activity was cancelled (fatal stream error, or a forced stop) with cleanup owed.

        A forced stop skips cleanup. So does a lease whose hooks are recorded:
        the next process owes it, from the record. Otherwise the afterLease hook
        still runs, bounded by stop_grace, as it did when the exporter shut down
        mid-lease.
        """
        stop = self._inputs.snapshot().stop
        if (stop is not None and stop.force) or lease.hooks.tracked:
            return
        with CancelScope(shield=True, deadline=anyio.current_time() + self.exporter.stop_grace):
            await self._clean_up(lease, generation)

    @asynccontextmanager
    async def _lease_connections(self, lease: LeaseContext, *, enabled: bool) -> AsyncGenerator[None, None]:
        """Bridge client dials for this lease while the block is open."""
        if not enabled:
            yield
            return
        exp = self.exporter
        listen_tx, listen_rx = create_memory_object_stream[jumpstarter_pb2.ListenResponse](max_buffer_size=10)
        async with listen_tx, listen_rx, create_task_group() as tg:
            tg.start_soon(exp._retry_stream, "Listen", exp._listen_stream_factory(lease.lease_name), listen_tx)

            async def bridge() -> None:
                # Dials buffered during setup are bridged only once setup is done,
                # and never for a lease that has already ended.
                await lease.before_lease_hook.wait()
                async for request in listen_rx:
                    if lease.has_ended():
                        break
                    logger.info(
                        "Handling new connection request on lease %s (router=%s)",
                        lease.lease_name,
                        request.router_endpoint,
                    )
                    tg.start_soon(
                        exp._handle_client_conn,
                        lease.socket_path,
                        request.router_endpoint,
                        request.router_token,
                        exp.tls,
                        exp.grpc_options,
                    )

            tg.start_soon(bridge)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
